"""Создание и просмотр складских накладных («Управление складом → Накладные»).

Phase 2: ручное создание обычной приходной накладной со строками номенклатуры и
флагом «персонал» на каждой строке. Строки — источник правды (``invoice_line_item``);
их минимальный срез зеркалится в ``SupplierInvoice.line_items`` (JSONB), чтобы
номенклатурный бартер-матчинг продолжал работать. Персонал-строки входят в полную
сумму накладной (``amount``) и в ``staff_amount``, но позже исключаются из пуша в iiko.

Push в iiko (Phase 3), сплит-оплата и ДДС-мэтч (Phase 4), явный бартер (Phase 5) —
отдельными слоями поверх этого сервиса.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BarterReturnLine,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    IikoProduct,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    User,
)
from app.services import supplier_service_periods as service_periods
from app.services.counterparty_matching import _allocated_amount, _recompute_status
from app.services.invoice_price_control import PriceCheckLine, apply_price_control

# Статьи ДДС для блока «Траты на персонал» накладной (выбор из двух — питание / прочие).
# По ИМЕНИ (code/uuid различны dev/prod), как whitelist чека.
STAFF_ARTICLE_NAMES = ("Расходы на питание персонала", "Расходы на персонал")

# Статья ДДС «Оплата поставщикам» — единый признак «товарной» строки: строка идёт в iiko
# приходом ТОЛЬКО если её статья = эта (или статьи нет вовсе — складская строка накладной).
# Любая другая статья (питание/расходы персонала, содержание точек и т.п.) — это РАСХОД, а
# не складской приход, и в iiko не отправляется. Чеки кладут статью в каждую строку
# (is_staff всегда false), накладные — флаг is_staff + статью только у персональных. Поэтому
# «товар vs расход» определяем по СТАТЬЕ, а не по is_staff — единый источник истины для
# пуша, бейджа и разбивки. По ИМЕНИ (code/uuid различны dev/prod), как whitelist чека и
# cheque_payout_push. См. project_card_purchase_invoice_gap.
SUPPLIER_PAYMENT_ARTICLE_NAME = "Оплата поставщикам"

# Статья ДДС «Аванс поставщику» — на неё вешаем дебиторку из излишка оплаты при коррекции
# оплаченной накладной (как create_supplier_prepayment). См. supplier_prepayments.PREPAYMENT_ARTICLE_CODE.
PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"


async def supplier_payment_article_ids(session: AsyncSession) -> set[uuid.UUID]:
    """UUID-ы статей «Оплата поставщикам» (товарных). Множество — каталог может содержать
    тёзку (сид + созданная статья); строка-товар может ссылаться на любую из них."""
    rows = await session.scalars(
        select(DdsArticle.id).where(DdsArticle.name == SUPPLIER_PAYMENT_ARTICLE_NAME)
    )
    return set(rows.all())


def line_is_goods(line: InvoiceLineItem, supplier_article_ids: set[uuid.UUID]) -> bool:
    """Товарная строка (идёт в iiko приходом): НЕ персонал И (без статьи ДДС или статья
    «Оплата поставщикам»). Накладная помечает персонал флагом is_staff (статья может быть
    пустой), чек — расходной статьёй (is_staff всегда false). Учитываем оба признака,
    иначе персональная строка накладной без статьи ложно считается товаром."""
    if line.is_staff:
        return False
    return line.dds_article_id is None or line.dds_article_id in supplier_article_ids


class WarehouseInvoiceError(RuntimeError):
    """Domain error for warehouse invoice creation (maps to HTTP 409/422)."""


async def list_staff_articles(session: AsyncSession) -> list[DdsArticle]:
    """Статьи ДДС блока «Траты на персонал» накладной (питание / прочие затраты)."""
    result = await session.scalars(
        select(DdsArticle)
        .where(
            DdsArticle.name.in_(STAFF_ARTICLE_NAMES),
            DdsArticle.is_active.is_(True),
            DdsArticle.movement_type == "outflow",
        )
        .order_by(DdsArticle.name)
    )
    return list(result.all())


async def invoice_permission_kind(session: AsyncSession, invoice: SupplierInvoice) -> str:
    """«barter» для бартерной накладной (явный ``barter_role`` или контрагент-бартер),
    иначе «normal». По нему роуты выбирают право invoices.normal.* / invoices.barter.*."""
    if invoice.barter_role is not None:
        return "barter"
    relationship = await session.scalar(
        select(CounterpartyPayableProfile.relationship).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    return "barter" if relationship == "barter" else "normal"


async def _ensure_barter_relationship(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """A counterparty we issue/receive a barter loan with is a barter partner.

    Ручной замок типа отношений (``relationship_manual``) не перебиваем: если владелец явно
    закрепил тип в карточке, менять его молча нельзя. Барт-роль накладной живёт в ``barter_role``
    независимо и на матчинг/права влияет сама по себе."""
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is None:
        session.add(
            CounterpartyPayableProfile(counterparty_id=counterparty_id, relationship="barter")
        )
        await session.flush()
    elif not profile.relationship_manual and profile.relationship != "barter":
        profile.relationship = "barter"


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Допуск перевозврата ТОВАРОМ: вернуть можно на 100 г больше выданного — при фасовке довесок
# обычное дело. Сверх допуска ввод отклоняется (заняли 3 кг, вводят 3,5 — это ошибка оператора,
# а не довесок). Долг от лишних граммов НЕ растёт: он номинирован товаром.
RETURN_QTY_TOLERANCE = Decimal("0.100")


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


@dataclass
class LineInput:
    name: str
    quantity: Decimal
    price: Decimal
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    is_staff: bool = False
    # Статья ДДС персональной строки — на неё ляжет «персонал»-часть при оплате.
    dds_article_id: uuid.UUID | None = None


def _assert_goods_have_product(
    lines: Sequence[LineInput], products: dict[uuid.UUID, IikoProduct]
) -> None:
    """Товарная строка (НЕ персонал и без расходной статьи ДДС) обязана быть сопоставлена с
    номенклатурой iiko.

    Иначе при выгрузке накладной в iiko строки без ``product_guid`` молча отбрасываются
    (``warehouse_invoice_push.prepare_push``: ``if not line.product_guid: continue``) — накладная
    уходит НЕПОЛНОЙ, сумма в iiko < нашей, и расхождение незаметно (статус остаётся ``pushed``).
    Поэтому требуем явный выбор товара уже на вводе. Персональные (``is_staff``) и расходные (со
    статьёй ДДС) строки в iiko не идут — для них сопоставление не требуется."""
    for line in lines:
        is_goods = not line.is_staff and line.dds_article_id is None
        if is_goods and (
            line.iiko_product_id is None or products.get(line.iiko_product_id) is None
        ):
            raise WarehouseInvoiceError(
                f"Позиция «{line.name or '—'}»: выберите конкретный товар из номенклатуры iiko. "
                "Товарную позицию нельзя сохранить без сопоставления — иначе она потеряется при "
                "выгрузке накладной в iiko."
            )


async def next_invoice_number(session: AsyncSession) -> str:
    """Следующий свободный целочисленный номер по НАШИМ накладным (manual + kassa_invoice).

    Раньше считался максимум только по ``source='manual'`` → автоподсказка для накладной
    Кассы повторяла уже занятый kassa-номер (например «4»), а iiko ключует документ по
    «номер + дата» и отвергает второй такой же за день. Теперь учитываем и kassa_invoice.
    Берём только нашу серию (manual/kassa_invoice) и только чисто-цифровые номера: серия
    чеков «Ч-N», iiko-номера поставщиков и суффиксы коллизий «N-2» (см.
    ``_unique_invoice_number``) в счёт не идут. Финальную уникальность на дату даёт
    ``_unique_invoice_number`` при создании — это лишь стартовая подсказка.
    """
    numbers = (
        await session.scalars(
            select(SupplierInvoice.number).where(
                SupplierInvoice.source.in_(("manual", "kassa_invoice")),
                SupplierInvoice.number.isnot(None),
            )
        )
    ).all()
    top = 0
    for raw in numbers:
        value = (raw or "").strip()
        if value.isdigit():
            top = max(top, int(value))
    return str(top + 1)


async def _unique_invoice_number(
    session: AsyncSession, base: str, on_date: date, *, exclude_id: uuid.UUID | None = None
) -> str:
    """Сделать номер уникальным в пределах ОДНОЙ ДАТЫ (ключ iiko = номер + дата).

    Если базовый номер уже занят другой накладной на эту же дату — добавляем суффикс
    «-2», «-3»… до первого свободного. Так два разных поставщика с «номером 4» в один
    день не конфликтуют при пуше в iiko. exclude_id — чтобы при правке не считать саму
    себя занятой.
    """
    base = (base or "").strip() or "1"
    candidate = base
    suffix = 2
    while True:
        query = select(SupplierInvoice.id).where(
            SupplierInvoice.number == candidate,
            SupplierInvoice.invoice_date == on_date,
        )
        if exclude_id is not None:
            query = query.where(SupplierInvoice.id != exclude_id)
        if await session.scalar(query) is None:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


async def create_warehouse_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    issued_at: datetime,
    lines: Sequence[LineInput],
    mode: str = "normal",
    we_lend: bool = True,
    number: str | None = None,
    due_date: date | None = None,
    store_guid: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    source: str = "manual",
) -> SupplierInvoice:
    if not lines:
        raise WarehouseInvoiceError("Добавьте хотя бы одну строку накладной")
    if issued_at is None:
        raise WarehouseInvoiceError("Укажите дату и время чека")

    product_ids = [line.iiko_product_id for line in lines if line.iiko_product_id]
    products: dict[uuid.UUID, IikoProduct] = {}
    if product_ids:
        rows = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        products = {product.id: product for product in rows}
    _assert_goods_have_product(lines, products)

    # Barter loan: расход (we_lend=мы выдаём) или приход (нам выдают); явная роль «loan».
    if mode == "loan":
        direction = "receivable" if we_lend else "payable"
        barter_role: str | None = "loan"
        barter_return_status: str | None = "open"
        await _ensure_barter_relationship(session, counterparty_id)
    else:
        direction = "payable"
        barter_role = None
        barter_return_status = None

    resolved_number = number or await next_invoice_number(session)
    # Накладные Кассы/ручные уходят в iiko, который ключует документ по «номер + дата».
    # Гарантируем уникальность номера на дату чека (бартер/iiko-синканные не трогаем).
    if mode != "loan" and source in ("manual", "kassa_invoice"):
        resolved_number = await _unique_invoice_number(session, resolved_number, issued_at.date())

    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source=source,
        direction=direction,
        doc_kind="closing",  # складская приходная накладная = приход товара = закрывающий
        barter_role=barter_role,
        barter_return_status=barter_return_status,
        number=resolved_number,
        invoice_date=issued_at.date(),
        issued_at=issued_at,
        due_date=due_date,
        amount=Decimal("0.00"),
        payment_status="unpaid",
        store_guid=store_guid,
        created_by_user_id=actor_user_id,
    )
    session.add(invoice)
    await session.flush()

    total = Decimal("0.00")
    staff_total = Decimal("0.00")
    vat_total = Decimal("0.00")
    mirror: list[dict[str, Any]] = []
    price_lines: list[PriceCheckLine] = []
    for index, line in enumerate(lines):
        product = products.get(line.iiko_product_id) if line.iiko_product_id else None
        quantity = _qty(line.quantity)
        price = _money(line.price)
        line_sum = _money(quantity * price)
        vat_sum: Decimal | None = None
        if line.vat_percent:
            rate = Decimal(str(line.vat_percent))
            vat_sum = _money(line_sum * rate / (Decimal("100") + rate))  # gross-inclusive
            vat_total += vat_sum
        item_name = line.name or (product.name if product else "Позиция")
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                iiko_product_id=line.iiko_product_id,
                product_guid=product.iiko_id if product else None,
                name=item_name,
                article=product.code if product else None,
                unit=product.unit if product else None,
                quantity=quantity,
                price=price,
                sum=line_sum,
                vat_percent=Decimal(str(line.vat_percent)) if line.vat_percent else None,
                vat_sum=vat_sum,
                is_staff=line.is_staff,
                # Статья ДДС — только у персональных строк (товарные идут по «Оплата
                # поставщикам» при оплате); фронт на товарных её не задаёт.
                dds_article_id=line.dds_article_id if line.is_staff else None,
                sort_order=index,
            )
        )
        total += line_sum
        if line.is_staff:
            staff_total += line_sum
        price_lines.append(
            PriceCheckLine(
                name=item_name,
                product_guid=product.iiko_id if product else None,
                unit=product.unit if product else None,
                price=price,
                is_staff=line.is_staff,
            )
        )
        # Mirror keeps product_id/article so counterparty_barter_match._products() works.
        mirror.append(
            {
                "product_id": product.iiko_id if product else None,
                "article": product.code if product else None,
                "name": item_name,
                "quantity": str(quantity),
                "amount": str(line_sum),
            }
        )

    invoice.amount = _money(total)
    invoice.staff_amount = _money(staff_total)
    invoice.vat_total = _money(vat_total)
    invoice.line_items = mirror
    # Контроль ошибочных цен: помечаем накладную «подозрительной», если цена позиции сильно
    # отклонилась от скользящего среднего по товару → оплата/банк заблокированы до подтверждения.
    await apply_price_control(session, invoice, price_lines)
    # Правило 2 канона: приходная — закрывающий документ, гасит открытую дебиторку контрагента
    # (FIFO; целевые goods-авансы и бартер исключены внутри). Канал «склад» раньше только ставил
    # ярлык doc_kind='closing' — аванс и приходная висели ДЗ/КЗ параллельно на одну поставку.
    # Хук строго ПОСЛЕ финализации amount (зачёт считает от остатка накладной).
    if invoice.direction == "payable" and invoice.barter_role is None:
        from app.services.supplier_prepayments import apply_closing_document

        await apply_closing_document(session, invoice, actor_user_id=actor_user_id)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def _rebuild_invoice_lines(
    session: AsyncSession,
    invoice: SupplierInvoice,
    lines: Sequence[LineInput],
    products: dict[uuid.UUID, IikoProduct],
) -> None:
    """Заменить строки накладной на ``lines`` и пересчитать amount/staff_amount/vat_total и
    JSONB-зеркало ``line_items``. Тот же расчёт, что в ``create_warehouse_invoice``, но со
    сносом старых строк. Общий для ``update_warehouse_invoice`` и ``adjust_paid_invoice``."""
    await session.execute(
        delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)
    )
    total = Decimal("0.00")
    staff_total = Decimal("0.00")
    vat_total = Decimal("0.00")
    mirror: list[dict[str, Any]] = []
    price_lines: list[PriceCheckLine] = []
    for index, line in enumerate(lines):
        product = products.get(line.iiko_product_id) if line.iiko_product_id else None
        quantity = _qty(line.quantity)
        price = _money(line.price)
        line_sum = _money(quantity * price)
        vat_sum: Decimal | None = None
        if line.vat_percent:
            rate = Decimal(str(line.vat_percent))
            vat_sum = _money(line_sum * rate / (Decimal("100") + rate))
            vat_total += vat_sum
        item_name = line.name or (product.name if product else "Позиция")
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                iiko_product_id=line.iiko_product_id,
                product_guid=product.iiko_id if product else None,
                name=item_name,
                article=product.code if product else None,
                unit=product.unit if product else None,
                quantity=quantity,
                price=price,
                sum=line_sum,
                vat_percent=Decimal(str(line.vat_percent)) if line.vat_percent else None,
                vat_sum=vat_sum,
                is_staff=line.is_staff,
                dds_article_id=line.dds_article_id if line.is_staff else None,
                sort_order=index,
            )
        )
        total += line_sum
        if line.is_staff:
            staff_total += line_sum
        price_lines.append(
            PriceCheckLine(
                name=item_name,
                product_guid=product.iiko_id if product else None,
                unit=product.unit if product else None,
                price=price,
                is_staff=line.is_staff,
            )
        )
        mirror.append(
            {
                "product_id": product.iiko_id if product else None,
                "article": product.code if product else None,
                "name": item_name,
                "quantity": str(quantity),
                "amount": str(line_sum),
            }
        )
    invoice.amount = _money(total)
    invoice.staff_amount = _money(staff_total)
    invoice.vat_total = _money(vat_total)
    invoice.line_items = mirror
    # Правка позиций пересчитывает контроль цен и сбрасывает прежнее подтверждение.
    await apply_price_control(session, invoice, price_lines)


async def update_warehouse_invoice(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    lines: Sequence[LineInput],
    issued_at: datetime | None = None,
    number: str | None = None,
) -> SupplierInvoice:
    """Replace an invoice's lines and recompute totals. Only for UNPAID, non-barter invoices
    (paid allocations are tied to the amount; barter has its own loan/return ledger). The lines
    are rebuilt exactly like ``create_warehouse_invoice``. iiko push state is reset to
    ``not_pushed`` (so a corrected invoice can be pushed) UNLESS it was already pushed — then
    ``external_id`` is kept so the round-trip dedup still recognises it (the stale iiko document
    is the owner's to delete + re-push)."""
    if not lines:
        raise WarehouseInvoiceError("Добавьте хотя бы одну строку накладной")
    if invoice.barter_role is not None:
        raise WarehouseInvoiceError("Бартерную накладную редактировать нельзя")
    if invoice.payment_status != "unpaid":
        raise WarehouseInvoiceError("Редактировать можно только неоплаченную накладную")
    if invoice.draft_id is not None:
        # Платёж уже уехал (черновик в банке / деньги на Сейфе под этот черновик) — правка сумм
        # разошлась бы с платежом/резервом. Сначала отзыв платежа.
        raise WarehouseInvoiceError("Накладная отправлена в банк — сначала отзовите платёж")

    product_ids = [line.iiko_product_id for line in lines if line.iiko_product_id]
    products: dict[uuid.UUID, IikoProduct] = {}
    if product_ids:
        rows = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        products = {product.id: product for product in rows}
    _assert_goods_have_product(lines, products)

    await _rebuild_invoice_lines(session, invoice, lines, products)
    if issued_at is not None:
        invoice.issued_at = issued_at
        invoice.invoice_date = issued_at.date()
    # Уникальность номера на дату (ключ iiko = номер+дата); себя занятой не считаем.
    # Пересчитываем при смене номера ИЛИ даты (перенос на дату, где номер уже занят).
    target_number = number if number is not None else invoice.number
    if target_number is not None and (number is not None or issued_at is not None):
        invoice.number = await _unique_invoice_number(
            session, target_number, invoice.invoice_date, exclude_id=invoice.id
        )
    # Ещё-не-выгруженную корректированную накладную снова можно отправить в iiko. Уже выгруженную
    # (external_id) синхронизирует правкой роут put_invoice → propagate_invoice_edit_to_iiko.
    if invoice.iiko_push_status != "pushed":
        invoice.iiko_push_status = "not_pushed"
        invoice.iiko_push_error = None
    await session.commit()
    await session.refresh(invoice)
    return invoice


def ensure_invoice_deletable(invoice: SupplierInvoice) -> None:
    """Гейты удаления накладной — те же, что у правки, плюс банк: оплаченную/отправленную в банк
    не удаляем (деньги уже двигались), бартер живёт своим леджером займов/возвратов."""
    if invoice.barter_role is not None:
        raise WarehouseInvoiceError("Бартерную накладную удалять нельзя")
    if invoice.payment_status != "unpaid":
        raise WarehouseInvoiceError("Удалять можно только неоплаченную накладную")
    if invoice.draft_id is not None:
        raise WarehouseInvoiceError("Накладная отправлена в банк — сначала отзовите платёж")


async def delete_warehouse_invoice(session: AsyncSession, invoice: SupplierInvoice) -> None:
    """Удалить накладную у нас (строки/связи — каскадами БД). Гейты — ``ensure_invoice_deletable``;
    удаление же iiko-документа — забота вызывающего (роут удаляет в iiko ПЕРЕД локальным
    удалением, иначе реверс-синк воскресит накладную из живого PROCESSED-документа)."""
    ensure_invoice_deletable(invoice)
    await session.delete(invoice)
    await session.commit()


async def _spill_overpayment_to_prepayment(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    excess: Decimal,
    actor_user_id: uuid.UUID | None,
) -> Decimal:
    """Уменьшить аллокации накладной на ``excess`` и перенести излишек оплаты в дебиторку.

    Деньги при этом НЕ двигаются и НЕ сторнируются (они уже ушли при оплате) — переклассифицируем
    уже потраченное: излишек из РЕАЛЬНЫХ денег (bank/cash) становится новой предоплатой
    ``SupplierPrepayment`` («поставщик нам должен», гасится будущими поставками) БЕЗ второй
    проводки; излишек, гасившийся из уже существующей предоплаты, возвращаем в неё (уменьшаем
    ``amount_settled``), не плодя новую. Возвращает сумму, ушедшую в НОВУЮ дебиторку."""
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.invoice_id == invoice.id)
            .order_by(InvoicePaymentAllocation.created_at)
        )
    ).all()
    remaining = _money(excess)
    real_money_excess = Decimal("0.00")
    for alloc in allocations:
        if remaining <= 0:
            break
        take = min(_money(alloc.amount), remaining)
        if alloc.source_kind == "prepayment" and alloc.prepayment_id is not None:
            prepayment = await session.get(SupplierPrepayment, alloc.prepayment_id)
            if prepayment is not None:
                settled = max(Decimal("0.00"), _money(prepayment.amount_settled) - take)
                prepayment.amount_settled = settled
                prepayment.status = (
                    "settled"
                    if settled >= _money(prepayment.amount)
                    else ("partially_settled" if settled > 0 else "open")
                )
        else:
            real_money_excess += take
        rest = _money(alloc.amount) - take
        if rest <= 0:
            await session.delete(alloc)
        else:
            alloc.amount = rest
        remaining -= take

    real_money_excess = _money(real_money_excess)
    if real_money_excess > 0:
        article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == PREPAYMENT_ARTICLE_CODE)
        )
        session.add(
            SupplierPrepayment(
                counterparty_id=invoice.counterparty_id,
                kind="goods",
                wallet_id=None,
                amount=real_money_excess,
                amount_settled=Decimal("0.00"),
                status="open",
                # Денежный факт остаётся на оплате накладной — новую проводку не создаём.
                cashflow_transaction_id=None,
                article_id=article_id,
                note=f"Излишек оплаты по накладной №{invoice.number or '—'} — перенесён в дебиторку",
                created_by_user_id=actor_user_id,
            )
        )
    return real_money_excess


async def _snapshot_invoice_goods(
    session: AsyncSession, invoice_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    """Товарные позиции накладной (то, что реально ушло в iiko) по ``product_guid``: суммарное
    кол-во + цена + имя. Снимаем ДО правки — для возврата в iiko дельты «старая − новая»."""
    rows = (
        await session.scalars(
            select(InvoiceLineItem).where(
                InvoiceLineItem.invoice_id == invoice_id,
                InvoiceLineItem.is_staff.is_(False),
                InvoiceLineItem.is_return.is_(False),
                InvoiceLineItem.product_guid.isnot(None),
            )
        )
    ).all()
    goods: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = goods.setdefault(
            row.product_guid,
            {"quantity": Decimal("0"), "price": _money(row.price), "name": row.name},
        )
        entry["quantity"] = _qty(entry["quantity"] + _qty(row.quantity))
    return goods


def _goods_from_lines(
    lines: Sequence[LineInput], products: dict[uuid.UUID, IikoProduct]
) -> dict[str, Decimal]:
    """Товарные позиции правки по ``product_guid``: суммарное кол-во (персонал/без товара — мимо)."""
    goods: dict[str, Decimal] = {}
    for line in lines:
        if line.is_staff:
            continue
        product = products.get(line.iiko_product_id) if line.iiko_product_id else None
        if product is None:
            continue
        goods[product.iiko_id] = _qty(goods.get(product.iiko_id, Decimal("0")) + _qty(line.quantity))
    return goods


def _serialize_goods_for_return(old_goods: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """ВСЕ старые товары в формате позиций возврата (полный разворот, а не дельта): по каждому
    ``product_guid`` исходное кол-во по исходной цене. Возврат целиком снимает старый приход со
    склада и создаёт долг поставщика; правильные товары заносит уже НОВАЯ приходная (см.
    ``book_correction_in_iiko``)."""
    out: list[dict[str, Any]] = []
    for guid, info in old_goods.items():
        qty = _qty(info["quantity"])
        if qty <= 0:
            continue
        out.append(
            {
                "product": guid,
                "quantity": float(qty),
                "price": float(info["price"]),
                "name": info["name"],
            }
        )
    return out


async def adjust_paid_invoice(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    lines: Sequence[LineInput],
    issued_at: datetime | None = None,
    number: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Исправить УЖЕ ОПЛАЧЕННУЮ (или частично оплаченную) обычную накладную: заменить позиции и
    пересчитать сумму. Если новая сумма МЕНЬШЕ уже применённой оплаты — излишек не «повисает» и
    не задваивает расход, а уходит в дебиторку поставщику (``_spill_overpayment_to_prepayment``):
    реально потраченные деньги остаются на месте, лишняя часть переносится в аванс «поставщик нам
    должен». Гейт — право ``invoices.normal.edit_paid`` (проверяется в роуте).

    iiko-документ этой правкой НЕ трогаем: оплаченную накладную iiko не даёт распровести, а
    ``add_payment`` необратим (проверено на живом API). Отражение коррекции в iiko — отдельным
    шагом (полный разворот: возврат всех старых товаров + новая приходная + зачёт со счёта
    «Задолженность перед поставщиками»); см. ``book_correction_in_iiko`` и
    project_edit_paid_invoice_feasibility."""
    if not lines:
        raise WarehouseInvoiceError("Добавьте хотя бы одну строку накладной")
    if invoice.barter_role is not None:
        raise WarehouseInvoiceError("Бартерную накладную редактировать нельзя")
    if invoice.payment_status not in ("paid", "partially_paid"):
        raise WarehouseInvoiceError(
            "Это правка ОПЛАЧЕННОЙ накладной — для неоплаченной используйте обычную правку"
        )
    if invoice.draft_id is not None:
        # Правку разрешаем, только если банк-платёж УЖЕ финализирован: черновик оплачен
        # (status='paid') и НЕ через Сейф. Тогда деньги ушли, излишек можно перенести в
        # дебиторку. Пока черновик висит в банке (created/updated/failed) или деньги
        # зарезервированы на Сейфе (pays_via_safe) — блокируем: иначе дебиторка разошлась бы
        # с висящим платежом/резервом (сначала отзыв платежа).
        draft = await session.get(CounterpartyPaymentDraft, invoice.draft_id)
        if draft is None or draft.status != "paid" or draft.pays_via_safe:
            raise WarehouseInvoiceError("Накладная отправлена в банк — сначала отзовите платёж")

    # Гард: не запускать коррекцию, пока оплата ОРИГИНАЛА не отражена в iiko. Иначе book_correction
    # перецепит external_id X→Y и затумбстонит X НАВСЕГДА — оплата X в iiko не появится, а возврат
    # уйдёт не на ту сумму (корень перекоса АЛЬЯНС ЮГ / DX001323A). Разблокировать можно кнопками
    # «Отправить в iiko сейчас» / «Подтверждено вручную» (в UI). Для не-iiko/без external_id гард
    # прозрачен (там банковского зеркала нет). См. project_iiko_supplier_balance_audit.
    from app.services.counterparty_iiko_payment import original_payment_settled_in_iiko

    if not await original_payment_settled_in_iiko(session, invoice):
        raise WarehouseInvoiceError(
            "Оплата этой накладной ещё не отражена в iiko. Пока оплата оригинала не проведена в "
            "iiko, исправлять нельзя — возврат уйдёт не на ту сумму. Отправьте оплату в iiko "
            "или подтвердите её вручную."
        )

    product_ids = [line.iiko_product_id for line in lines if line.iiko_product_id]
    products: dict[uuid.UUID, IikoProduct] = {}
    if product_ids:
        rows = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        products = {product.id: product for product in rows}
    _assert_goods_have_product(lines, products)

    allocated = await _allocated_amount(session, invoice.id)
    # Снимок товаров и суммы ДО правки — для отражения коррекции в iiko (Фаза 2, полный разворот).
    old_goods = await _snapshot_invoice_goods(session, invoice.id)
    amount_before = _money(invoice.amount)

    await _rebuild_invoice_lines(session, invoice, lines, products)
    new_amount = _money(invoice.amount)
    if new_amount <= 0:
        raise WarehouseInvoiceError("Сумма накладной должна быть больше нуля")

    if issued_at is not None:
        invoice.issued_at = issued_at
        invoice.invoice_date = issued_at.date()
    target_number = number if number is not None else invoice.number
    if target_number is not None and (number is not None or issued_at is not None):
        invoice.number = await _unique_invoice_number(
            session, target_number, invoice.invoice_date, exclude_id=invoice.id
        )

    # Излишек оплаты (было применено больше, чем стоит исправленная накладная) → дебиторка.
    excess = _money(allocated - new_amount)
    if excess > 0:
        await _spill_overpayment_to_prepayment(
            session, invoice, excess=excess, actor_user_id=actor_user_id
        )

    # Отражение коррекции в iiko (Фаза 2) — ПОЛНЫЙ разворот: возврат всех старых товаров + новая
    # приходная + зачёт (проводку делает роут через book_correction_in_iiko). Копим снимок старых
    # товаров для возврата. Запускаем контур только если реально что-то изменилось (товары ИЛИ
    # сумма/цена) и накладная есть в iiko — иначе no-op (повторное «исправить» без правок не плодит
    # документы).
    if invoice.external_id:
        old_goods_qty = {guid: _qty(info["quantity"]) for guid, info in old_goods.items()}
        goods_changed = old_goods_qty != _goods_from_lines(lines, products)
        if goods_changed or amount_before != new_amount:
            invoice.iiko_return_lines = _serialize_goods_for_return(old_goods)
            invoice.iiko_return_status = "pending"

    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def list_warehouse_invoices(
    session: AsyncSession,
    *,
    statuses: Sequence[str] | None = None,
    counterparty_id: uuid.UUID | None = None,
    has_staff: bool | None = None,
) -> list[dict[str, Any]]:
    allocated_subq = (
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0))
        .where(InvoicePaymentAllocation.invoice_id == SupplierInvoice.id)
        .scalar_subquery()
    )
    query = (
        select(SupplierInvoice, Counterparty.name, allocated_subq.label("allocated"))
        .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .order_by(
            SupplierInvoice.issued_at.nulls_last(),
            SupplierInvoice.invoice_date.nulls_last(),
        )
    )
    if statuses:
        query = query.where(SupplierInvoice.payment_status.in_(tuple(statuses)))
    if counterparty_id is not None:
        query = query.where(SupplierInvoice.counterparty_id == counterparty_id)
    if has_staff is True:
        query = query.where(SupplierInvoice.staff_amount > 0)

    rows = list(await session.execute(query))
    return [_invoice_summary(invoice, name, allocated) for invoice, name, allocated in rows]


def _invoice_summary(
    invoice: SupplierInvoice, counterparty_name: str, allocated: Any = 0
) -> dict[str, Any]:
    amount = _money(invoice.amount)
    staff = _money(invoice.staff_amount)
    allocated_money = _money(allocated)
    return {
        "id": str(invoice.id),
        "counterparty_id": str(invoice.counterparty_id),
        "counterparty_name": counterparty_name,
        "source": invoice.source,
        "direction": invoice.direction,
        "number": invoice.number,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "amount": float(amount),
        "staff_amount": float(staff),
        "production_amount": float(amount - staff),
        "allocated": float(allocated_money),
        "remaining": float(amount - allocated_money),
        "payment_status": invoice.payment_status,
        # Черновик в банке: оплата уже уехала — повторную оплату в UI не предлагать.
        "draft_id": str(invoice.draft_id) if invoice.draft_id else None,
        "iiko_push_status": invoice.iiko_push_status,
        # iiko documentId — фронту для предупреждения «удалится и в iiko».
        "external_id": invoice.external_id,
        "barter_role": invoice.barter_role,
        "barter_return_status": invoice.barter_return_status,
        # Контроль цен: clean/flagged/confirmed — списку для бейджа «⚠ проверить цены».
        "price_control_status": invoice.price_control_status,
    }


async def get_warehouse_invoice(
    session: AsyncSession, invoice_id: uuid.UUID
) -> dict[str, Any] | None:
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        return None
    counterparty = await session.get(Counterparty, invoice.counterparty_id)
    lines = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == invoice_id)
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.invoice_id == invoice_id)
            .order_by(InvoicePaymentAllocation.created_at)
        )
    ).all()
    allocated = sum((_money(a.amount) for a in allocations), Decimal("0.00"))
    summary = _invoice_summary(invoice, counterparty.name if counterparty else "—", allocated)
    summary["due_date"] = invoice.due_date.isoformat() if invoice.due_date else None
    summary["iiko_push_error"] = invoice.iiko_push_error
    # Статус возврата коррекции в iiko (Фаза 2) — фронту для бейджа/кнопки «Повторить возврат».
    summary["iiko_return_status"] = invoice.iiko_return_status
    summary["iiko_return_external_id"] = invoice.iiko_return_external_id
    summary["iiko_return_error"] = invoice.iiko_return_error
    # Статус привязанного черновика — фронту для «Отправлено в банк» / «Деньги в Сейфе».
    draft = (
        await session.get(CounterpartyPaymentDraft, invoice.draft_id)
        if invoice.draft_id
        else None
    )
    summary["draft_status"] = draft.status if draft else None
    summary["draft_pays_via_safe"] = bool(draft.pays_via_safe) if draft else False
    # Период оказания услуги: нужен ли он этому контрагенту и заполнен ли. Накладная не из
    # почты рождается без периода — тогда карточка даёт его ввести (иначе счёт не уйдёт в банк).
    period_required = bool(
        await session.scalar(
            select(CounterpartyPayableProfile.service_period_required).where(
                CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
            )
        )
    )
    summary["service_period_required"] = period_required
    summary["service_period_status"] = service_periods.effective_period_status(
        invoice.service_period_status, required=period_required
    )
    summary["service_period_start"] = (
        invoice.service_period_start.isoformat() if invoice.service_period_start else None
    )
    summary["service_period_end"] = (
        invoice.service_period_end.isoformat() if invoice.service_period_end else None
    )
    # Контроль ошибочных цен: статус + снимок аномалий (для баннера и блокировки оплаты).
    anomalies = list(invoice.price_anomalies or [])
    summary["price_control_status"] = invoice.price_control_status
    summary["price_anomalies"] = anomalies
    summary["price_confirmed_at"] = (
        invoice.price_confirmed_at.isoformat() if invoice.price_confirmed_at else None
    )
    if invoice.price_confirmed_by_user_id:
        confirmer = await session.get(User, invoice.price_confirmed_by_user_id)
        summary["price_confirmed_by"] = confirmer.full_name if confirmer else None
    else:
        summary["price_confirmed_by"] = None
    # Аномалии по (product_guid, unit) → аннотация конкретных строк (подсветка цены в UI).
    anomaly_by_key = {(a.get("product_guid"), a.get("unit")): a for a in anomalies}

    # Гард правки оплаченной: отражена ли оплата оригинала в iiko (иначе правку не пускаем — возврат
    # уйдёт не на ту сумму) и можно ли отправить оплату в iiko автоматически (однонакладный банк).
    from app.services.counterparty_iiko_payment import (
        iiko_invoice_payment_auto_sendable,
        original_payment_settled_in_iiko,
    )

    summary["iiko_payment_settled"] = await original_payment_settled_in_iiko(session, invoice)
    summary["iiko_payment_auto_sendable"] = await iiko_invoice_payment_auto_sendable(
        session, invoice
    )

    # Единая логика «товар vs расход» по статье ДДС (а не по is_staff): чеки кладут статью
    # «питание персонала» в строку, оставляя is_staff=false — нельзя считать такие строки
    # товаром. Названия статей нужны фронту для бейджа.
    supplier_article_ids = await supplier_payment_article_ids(session)
    article_ids = {line.dds_article_id for line in lines if line.dds_article_id is not None}
    article_names: dict[uuid.UUID, str] = {}
    if article_ids:
        rows_a = await session.execute(
            select(DdsArticle.id, DdsArticle.name).where(DdsArticle.id.in_(article_ids))
        )
        article_names = {aid: name for aid, name in rows_a.all()}
    summary["allocations"] = [
        {
            "id": str(a.id),
            "source_kind": a.source_kind,
            "bank_operation_id": str(a.bank_operation_id) if a.bank_operation_id else None,
            "cashflow_transaction_id": (
                str(a.cashflow_transaction_id) if a.cashflow_transaction_id else None
            ),
            "amount": float(_money(a.amount)),
        }
        for a in allocations
    ]
    summary["lines"] = [
        {
            "id": str(line.id),
            "name": line.name,
            "article": line.article,
            "unit": line.unit,
            "quantity": float(_qty(line.quantity)),
            "price": float(_money(line.price)),
            "sum": float(_money(line.sum)),
            "vat_percent": float(line.vat_percent) if line.vat_percent is not None else None,
            "is_staff": line.is_staff,
            # Расходная строка (не товар → не уходит в iiko): по статье ДДС, единообразно
            # для чеков и накладных. Название статьи — для бейджа на фронте.
            "is_expense": not line_is_goods(line, supplier_article_ids),
            # Позиция возвращена в магазин (чек Кассы): хранится для gross-следа,
            # но не проведена — фронт показывает красной строкой.
            "is_return": line.is_return,
            "dds_article_name": (
                article_names.get(line.dds_article_id) if line.dds_article_id else None
            ),
            # Для редактирования: сохранить связь с номенклатурой iiko и статьёй персонала.
            "iiko_product_id": str(line.iiko_product_id) if line.iiko_product_id else None,
            "dds_article_id": str(line.dds_article_id) if line.dds_article_id else None,
            # Контроль цен: если цена строки аномальна — среднее/отклонение/направление
            # (high=дорого, low=дёшево) для подсветки конкретной ячейки цены. Иначе None.
            "price_flag": (
                anomaly_by_key.get((line.product_guid, line.unit), {}).get("direction")
            ),
            "price_avg": (
                anomaly_by_key.get((line.product_guid, line.unit), {}).get("avg_price")
            ),
            "price_deviation_pct": (
                anomaly_by_key.get((line.product_guid, line.unit), {}).get("deviation_pct")
            ),
        }
        for line in lines
    ]
    # Разбивка «товар / расход» по статье ДДС (чек с is_staff=false тоже разносится верно).
    # Возвращённые позиции не проведены — не входят ни в товар, ни в расход.
    if lines:
        expense_sum = sum(
            (
                _money(line.sum)
                for line in lines
                if not line.is_return and not line_is_goods(line, supplier_article_ids)
            ),
            Decimal("0.00"),
        )
        summary["staff_amount"] = float(expense_sum)
        summary["production_amount"] = float(_money(invoice.amount) - expense_sum)
        summary["returned_total"] = float(
            sum((_money(line.sum) for line in lines if line.is_return), Decimal("0.00"))
        )
    return summary


@dataclass
class ReturnLineInput:
    amount: Decimal
    loan_line_item_id: uuid.UUID | None = None
    quantity: Decimal | None = None


async def _loan_returned_amount(session: AsyncSession, loan_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(BarterReturnLine.amount), 0)).where(
            BarterReturnLine.loan_invoice_id == loan_id
        )
    )
    return _money(total)


async def loan_settled_value(session: AsyncSession, loan: SupplierInvoice) -> Decimal:
    """Погашенная часть займа В РУБЛЯХ ЗАЙМА — по ИСХОДНЫМ ценам выдачи.

    Долг бартерного займа номинирован ТОВАРОМ (кг), рублёвая сумма займа — лишь его оценка по
    ценам выдачи. Поэтому строка гашения с количеством засчитывается как qty × исходная цена
    строки займа, сколько бы денег ни пришло фактически (гашение деньгами идёт по ТЕКУЩЕЙ
    цене — вернули 1 кг моцареллы деньгами по 350 при выдаче по 300 → долг закрыт на 300,
    деньги 350). Строка без товара (свободная сумма) — по фактической сумме. Для ПРИХОДНОГО
    займа (мы должны) добавляются обычные денежные оплаты накладной (аллокации): «оплатить
    как обычную поставку». Товарные возвраты по исходной цене дают qty × price == amount,
    поэтому для них формула совпадает с прежней рублёвой."""
    qty_rows = (
        await session.execute(
            select(
                BarterReturnLine.loan_line_item_id,
                BarterReturnLine.quantity,
                BarterReturnLine.amount,
                BarterReturnLine.return_invoice_id,
                InvoiceLineItem.price,
                InvoiceLineItem.quantity,
                InvoiceLineItem.sum,
            )
            .join(InvoiceLineItem, InvoiceLineItem.id == BarterReturnLine.loan_line_item_id)
            .where(
                BarterReturnLine.loan_invoice_id == loan.id,
                BarterReturnLine.loan_line_item_id.isnot(None),
                BarterReturnLine.quantity.isnot(None),
            )
        )
    ).all()
    # Построчно, с замыканием «сумма строки — эталон»: товарный возврат несёт точную сумму,
    # денежное гашение зачитывается как кг × исходная цена, а строка, погашенная по кг
    # ЦЕЛИКОМ, зачитывается ровно своей суммой — иначе копеечный дрейф округлений
    # (Σ округлений ≠ округление суммы) никогда не даст остатку дойти до нуля.
    per_line: dict[uuid.UUID, dict[str, Decimal]] = {}
    for line_id, qty, amount, return_invoice_id, price, line_qty, line_sum in qty_rows:
        bucket = per_line.setdefault(
            line_id,
            {
                "qty": Decimal("0"),
                "credit": Decimal("0.00"),
                "line_qty": _qty(line_qty),
                "line_sum": _money(line_sum),
            },
        )
        bucket["qty"] += _qty(qty)
        bucket["credit"] += (
            _money(amount) if return_invoice_id is not None else _money(_qty(qty) * _money(price))
        )
    settled = Decimal("0.00")
    for bucket in per_line.values():
        if _qty(bucket["qty"]) >= bucket["line_qty"]:
            settled += bucket["line_sum"]
        else:
            settled += bucket["credit"]
    free_money = await session.scalar(
        select(func.coalesce(func.sum(BarterReturnLine.amount), 0)).where(
            BarterReturnLine.loan_invoice_id == loan.id,
            BarterReturnLine.loan_line_item_id.is_(None),
        )
    )
    settled += _money(free_money)
    if loan.direction == "payable":
        allocated = await session.scalar(
            select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                InvoicePaymentAllocation.invoice_id == loan.id
            )
        )
        settled += _money(allocated)
    # Прощённый недовес — такая же часть закрытого долга, как товар и деньги: без него заём
    # с недовозвратом навсегда остался бы partially_returned с копеечной кредиторкой.
    settled += _money(loan.barter_writeoff_amount)
    # Зачёт НЕ БОЛЬШЕ суммы займа: возврат не делает нас кредитором (правило владельца).
    # Клэмп именно здесь, а не только на сумме возвратной накладной: при СМЕШАННОМ гашении
    # (часть товаром, часть деньгами) километры и рубли считаются разными путями, и без
    # общего потолка остаток уходил в минус — то есть возврат рождал дебиторку.
    return min(settled, _money(loan.amount))


async def sync_barter_loan_status(session: AsyncSession, loan: SupplierInvoice) -> Decimal:
    """Пересчитать статус займа от зачётной стоимости гашений. Возвращает остаток.

    ``returned`` (и ``payment_status='paid'``) — когда зачтено не меньше суммы займа;
    иначе ``partially_returned`` при ненулевом зачёте. Единая точка для товарных возвратов,
    денежных гашений и оплат приходного займа."""
    settled = await loan_settled_value(session, loan)
    remaining = _money(loan.amount) - settled
    if remaining <= 0:
        loan.barter_return_status = "returned"
        loan.payment_status = "paid"
    elif settled > 0:
        # В т.ч. ОТКАТ из returned: сняли денежную аллокацию (исключили банк-операцию) —
        # заём снова частично открыт, иначе долг занижен фантомно-закрытым займом.
        loan.barter_return_status = "partially_returned"
    else:
        loan.barter_return_status = "open"
    await session.flush()
    return max(remaining, Decimal("0.00"))


async def _loan_line_returned_qty(
    session: AsyncSession, loan_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    rows = await session.execute(
        select(
            BarterReturnLine.loan_line_item_id,
            func.coalesce(func.sum(BarterReturnLine.quantity), 0),
        )
        .where(
            BarterReturnLine.loan_invoice_id == loan_id,
            BarterReturnLine.loan_line_item_id.isnot(None),
        )
        .group_by(BarterReturnLine.loan_line_item_id)
    )
    return {line_id: _qty(qty) for line_id, qty in rows}


async def list_open_loans(
    session: AsyncSession, counterparty_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Open / partially-returned barter loans, with the remaining balance to settle."""
    query = (
        select(SupplierInvoice, Counterparty.name)
        .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .where(
            SupplierInvoice.barter_role == "loan",
            SupplierInvoice.barter_return_status != "returned",
        )
        .order_by(SupplierInvoice.issued_at.nulls_last())
    )
    if counterparty_id is not None:
        query = query.where(SupplierInvoice.counterparty_id == counterparty_id)
    out: list[dict[str, Any]] = []
    for loan, name in list(await session.execute(query)):
        # Зачётная стоимость: qty-гашения по исходной цене + свободные суммы + (для приходного
        # займа) денежные оплаты накладной — иначе оплаченный/гашёный деньгами заём висел бы
        # открытым полной суммой.
        returned = await loan_settled_value(session, loan)
        out.append(
            {
                "id": str(loan.id),
                "counterparty_id": str(loan.counterparty_id),
                "counterparty_name": name,
                "number": loan.number,
                "issued_at": loan.issued_at.isoformat() if loan.issued_at else None,
                "we_lend": loan.direction == "receivable",
                "amount": float(_money(loan.amount)),
                "returned": float(returned),
                "remaining": float(_money(loan.amount - returned)),
                "status": loan.barter_return_status,
            }
        )
    return out


async def get_loan_returnable(session: AsyncSession, loan_id: uuid.UUID) -> dict[str, Any] | None:
    loan = await session.get(SupplierInvoice, loan_id)
    if loan is None or loan.barter_role != "loan":
        return None
    lines = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == loan_id)
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    returned_qty = await _loan_line_returned_qty(session, loan_id)
    settled_total = await loan_settled_value(session, loan)
    last_prices = await _last_purchase_prices(
        session, [line.product_guid for line in lines if line.product_guid]
    )
    return {
        "id": str(loan.id),
        "number": loan.number,
        "we_lend": loan.direction == "receivable",
        "amount": float(_money(loan.amount)),
        "remaining": float(max(_money(loan.amount) - settled_total, Decimal("0.00"))),
        "lines": [
            {
                "id": str(line.id),
                "name": line.name,
                "unit": line.unit,
                "price": float(_money(line.price)),
                "quantity": float(_qty(line.quantity)),
                "remaining_qty": float(
                    _qty(line.quantity) - returned_qty.get(line.id, Decimal("0"))
                ),
                # Подсказка ТЕКУЩЕЙ цены для денежного гашения (решение владельца 18.07):
                # последняя закупочная из приходных; вводит всё равно оператор.
                "last_purchase_price": (
                    float(last_prices[line.product_guid])
                    if line.product_guid and line.product_guid in last_prices
                    else None
                ),
            }
            for line in lines
        ],
    }


async def _last_purchase_prices(
    session: AsyncSession, product_guids: list[str]
) -> dict[str, Decimal]:
    """Последняя закупочная цена по товарам — из строк ПРИХОДНЫХ накладных (не бартер),
    свежайшая по дате документа. Подсказка оператору при денежном гашении займа."""
    if not product_guids:
        return {}
    rows = (
        await session.execute(
            select(
                InvoiceLineItem.product_guid, InvoiceLineItem.price, SupplierInvoice.invoice_date
            )
            .join(SupplierInvoice, SupplierInvoice.id == InvoiceLineItem.invoice_id)
            .where(
                InvoiceLineItem.product_guid.in_(product_guids),
                SupplierInvoice.direction == "payable",
                SupplierInvoice.barter_role.is_(None),
                SupplierInvoice.payment_status != "void",
            )
            .order_by(SupplierInvoice.invoice_date.desc().nulls_last())
        )
    ).all()
    prices: dict[str, Decimal] = {}
    for guid, price, _inv_date in rows:
        if guid not in prices:
            prices[guid] = _money(price)
    return prices


async def convert_invoice_to_barter_loan(
    session: AsyncSession, invoice_id: uuid.UUID
) -> SupplierInvoice:
    """Признать унаследованную бартерную накладную ЯВНЫМ займом (модель владельца 18.07).

    Старый поток тащил встречные поставки из iiko без роли (barter_role IS NULL) — их гашение
    работало только хроно-зачётом равных сумм. Целевая модель: весь бартер — явные займы,
    гасятся товаром/деньгами из карточки. Эта конвертация — мостик для уже приехавших строк:
    приходная → заём «нам выдали», расходная → заём «мы выдали». После неё накладная уходит из
    хроно-зачёта (его фильтр barter_role IS NULL) и получает штатное гашение.

    Товарного движения и денег НЕ создаёт — только роль. Строки для возврата по позициям берём
    существующие; у совсем старых накладных их нет — материализуем из JSONB-зеркала
    (price = сумма/кол-во), иначе гашение по кг было бы невозможно."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehouseInvoiceError("Накладная не найдена")
    if invoice.barter_role is not None:
        raise WarehouseInvoiceError("Накладная уже в бартерном леджере")
    if invoice.payment_status not in ("unpaid", "partially_paid"):
        raise WarehouseInvoiceError("Займом можно признать только неоплаченную накладную")
    if invoice.barter_settlement_id is not None:
        raise WarehouseInvoiceError("Накладная уже зачтена встречной — займом её не признать")
    if invoice.draft_id is not None:
        raise WarehouseInvoiceError("Накладная отправлена в банк — сначала отзовите платёж")

    has_lines = (
        await session.scalar(
            select(func.count()).select_from(InvoiceLineItem).where(
                InvoiceLineItem.invoice_id == invoice.id
            )
        )
    ) or 0
    if not has_lines:
        created = 0
        for index, item in enumerate(invoice.line_items or []):
            quantity = _qty(item.get("quantity"))
            amount = _money(item.get("amount"))
            if quantity <= 0 or amount <= 0:
                continue  # мусорная строка зеркала — в основание долга не годится
            session.add(
                InvoiceLineItem(
                    invoice_id=invoice.id,
                    product_guid=item.get("product_id"),
                    name=item.get("name") or "Позиция",
                    article=item.get("article"),
                    quantity=quantity,
                    price=_money(amount / quantity),
                    sum=amount,
                    sort_order=index,
                )
            )
            created += 1
        await session.flush()
        # Считаем СОЗДАННЫЕ строки, а не длину зеркала: зеркало из одних мусорных строк дало
        # бы заём без позиций — его нельзя было бы погасить по килограммам никогда.
        has_lines = created
    if not has_lines:
        raise WarehouseInvoiceError(
            "У накладной нет позиций — гашение по килограммам невозможно"
        )

    invoice.barter_role = "loan"
    invoice.barter_return_status = "open"
    await _ensure_barter_relationship(session, invoice.counterparty_id)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def create_barter_return(
    session: AsyncSession,
    *,
    loan_id: uuid.UUID,
    issued_at: datetime,
    returns: Sequence[ReturnLineInput],
    number: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    write_off_remainder: bool = False,
) -> SupplierInvoice:
    """Возврат товара по бартерному займу.

    Долг номинирован ТОВАРОМ, поэтому арифметика идёт от килограммов:
    * вернуть можно на ``RETURN_QTY_TOLERANCE`` больше выданного (довесок при фасовке) —
      сверх этого ошибка; лишние граммы долг НЕ увеличивают (уходят по более низкой цене);
    * вернули меньше — хвост либо остаётся долгом, либо списывается
      (``write_off_remainder=True``: «про эти 60 грамм просто забываю»).
    """
    loan = await session.get(SupplierInvoice, loan_id)
    if loan is None or loan.barter_role != "loan":
        raise WarehouseInvoiceError("Заём не найден")
    if loan.draft_id is not None:
        # Платёж по займу уже в банке на подписи: вернуть товар сейчас — значит закрыть заём
        # без аллокации, а когда банк исполнит платёж, paid-переход заплатит ВТОРОЙ раз.
        # И товар у нас, и деньги ушли. Тот же гард стоит на прочих дверях этого файла.
        raise WarehouseInvoiceError(
            "Заём отправлен в банк — сначала отзовите платёж, потом оформляйте возврат"
        )
    if loan.barter_return_status == "returned":
        raise WarehouseInvoiceError("Заём уже полностью возвращён")
    items = [r for r in returns if _money(r.amount) > 0]
    if not items:
        raise WarehouseInvoiceError("Укажите, что возвращаете")

    loan_lines = {
        line.id: line
        for line in (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan_id)
            )
        ).all()
    }
    returned_qty = await _loan_line_returned_qty(session, loan_id)
    # Уже зачтённое ПО СТРОКАМ в рублях займа: товарные возвраты — их фактические суммы,
    # денежные qty-гашения — кг × исходная цена. Нужно для точного замыкания строки
    # (см. «сумма строки — эталон» ниже).
    line_credited: dict[uuid.UUID, Decimal] = {}
    prior_rows = (
        await session.scalars(
            select(BarterReturnLine).where(
                BarterReturnLine.loan_invoice_id == loan_id,
                BarterReturnLine.loan_line_item_id.isnot(None),
            )
        )
    ).all()
    for prior in prior_rows:
        line = loan_lines.get(prior.loan_line_item_id)
        if line is None:
            continue
        credit = (
            _money(prior.amount)
            if prior.return_invoice_id is not None
            else _money(_qty(prior.quantity) * _money(line.price))
        )
        line_credited[line.id] = line_credited.get(line.id, Decimal("0.00")) + credit
    # Остаток — по зачётной стоимости (qty × исходная цена + свободные суммы + оплаты
    # приходного займа), не по сырой Σ amount: денежные гашения по текущей цене несут
    # amount ≠ qty × исходная цена и сломали бы рублёвую арифметику.
    loan_remaining = _money(loan.amount) - await loan_settled_value(session, loan)

    # Сумма возврата ТОВАРОМ = кол-во × ИСХОДНАЯ цена займа: товар возвращается на склад
    # по той же стоимости, по которой ушёл, даже если рыночная цена изменилась. Возврат
    # ДЕНЬГАМИ (строка без товара) — переданная сумма денег как есть.
    resolved: list[tuple[InvoiceLineItem | None, Decimal | None, Decimal]] = []
    for r in items:
        line = loan_lines.get(r.loan_line_item_id) if r.loan_line_item_id else None
        if line is None and r.loan_line_item_id is not None:
            # Строка не из этого займа: молча трактовать её как свободную денежную сумму
            # нельзя — так списался бы долг суммой, не привязанной ни к какому товару.
            raise WarehouseInvoiceError("Позиция возврата не принадлежит этому займу")
        if line is not None:
            if r.quantity is None:
                raise WarehouseInvoiceError(f"Укажите количество для «{line.name}»")
            qty = _qty(r.quantity)
            if qty <= 0:
                raise WarehouseInvoiceError(f"Количество по «{line.name}» должно быть > 0")
            already = returned_qty.get(line.id, Decimal("0"))
            if _qty(already + qty) > _qty(line.quantity) + RETURN_QTY_TOLERANCE:
                raise WarehouseInvoiceError(
                    f"Возврат по «{line.name}» превышает выданное больше чем на "
                    f"{RETURN_QTY_TOLERANCE} кг: выдано {_qty(line.quantity)}, "
                    f"уже вернули {already}, вводите {qty}"
                )
            # Копим В СНИМКЕ: две строки запроса по одной позиции займа иначе прошли бы лимит
            # каждая по отдельности и вернули бы больше выданного.
            returned_qty[line.id] = _qty(already + qty)
            # СУММА СТРОКИ — ЭТАЛОН (канон round-trip накладных): при возврате ПОСЛЕДНИХ кг
            # строки закрываем ровно её рублёвый остаток (сумма − уже зачтённое), а не
            # qty × округлённая цена — иначе копеечный дрейф (2.6 кг × 470.48 = 1223.25 при
            # строке 1223.24) не даст закрыть заём никогда.
            # ПЕРЕВОЗВРАТ (в пределах допуска) идёт по тому же правилу: долг номинирован
            # ТОВАРОМ, поэтому лишние граммы не увеличивают сумму — они просто уходят по
            # более низкой цене за кг, и дебиторки из возврата не возникает.
            if _qty(already + qty) >= _qty(line.quantity):
                amount = max(
                    _money(line.sum) - line_credited.get(line.id, Decimal("0.00")),
                    Decimal("0.00"),
                )
            else:
                amount = _money(qty * line.price)
            resolved.append((line, qty, amount))
        else:
            resolved.append((None, None, _money(r.amount)))

    new_total = _money(sum((amt for _, _, amt in resolved), Decimal("0.00")))
    if new_total <= 0:
        raise WarehouseInvoiceError("Сумма возврата должна быть больше нуля")
    # Возврат сверх ДЕНЕЖНОГО остатка долга отклоняем, а не режем молча. Килограммы деньги не
    # расходуют (леджер их не видит), поэтому после частичной ОПЛАТЫ займа товара «на складе
    # займа» числится больше, чем мы должны: без этой проверки оператор вернул бы весь товар
    # поверх уже уплаченных денег — переплата не стала бы ни дебиторкой, ни предоплатой, а
    # возвратная накладная разъехалась бы с зеркалом iiko (там сумма строк, у нас — обрезанная).
    # Порог допуска в рублях — цена довеска: 100 г по цене самой дорогой строки возврата.
    price_cap = max(
        (_money(line.price) for line, _q, _a in resolved if line is not None),
        default=Decimal("0"),
    )
    money_tolerance = _money(RETURN_QTY_TOLERANCE * price_cap)
    if new_total > loan_remaining + money_tolerance:
        raise WarehouseInvoiceError(
            f"Возврат на {new_total} превышает остаток долга {loan_remaining}: "
            "часть займа уже погашена деньгами"
        )
    if new_total > loan_remaining:
        # Довесок в пределах допуска: принимаем товар, но кредиторами не становимся. Срезаем
        # НЕ только шапку: строки леджера, позиции накладной и зеркало iiko собираются из
        # ``resolved`` — иначе документ разъехался бы сам с собой (шапка одна, сумма строк
        # другая) и в iiko ушла бы третья цифра. Излишек снимаем с последней товарной строки.
        excess = new_total - loan_remaining
        for idx in range(len(resolved) - 1, -1, -1):
            if excess <= 0:
                break
            line, qty, amount = resolved[idx]
            take = min(amount, excess)
            resolved[idx] = (line, qty, _money(amount - take))
            excess -= take
        new_total = loan_remaining

    ret = SupplierInvoice(
        counterparty_id=loan.counterparty_id,
        source="manual",
        direction="payable" if loan.direction == "receivable" else "receivable",
        doc_kind="closing",  # бартерный возврат товара = закрывающий
        barter_role="return",
        barter_loan_id=loan.id,
        number=number or await next_invoice_number(session),
        invoice_date=issued_at.date(),
        issued_at=issued_at,
        amount=new_total,
        payment_status="paid",
        created_by_user_id=actor_user_id,
    )
    session.add(ret)
    await session.flush()

    mirror: list[dict[str, Any]] = []
    for idx, (line, qty, amount) in enumerate(resolved):
        session.add(
            BarterReturnLine(
                return_invoice_id=ret.id,
                loan_invoice_id=loan.id,
                loan_line_item_id=line.id if line else None,
                quantity=qty,
                amount=amount,
                created_by_user_id=actor_user_id,
            )
        )
        if line is not None:
            session.add(
                InvoiceLineItem(
                    invoice_id=ret.id,
                    iiko_product_id=line.iiko_product_id,
                    product_guid=line.product_guid,
                    name=line.name,
                    article=line.article,
                    unit=line.unit,
                    quantity=qty if qty is not None else Decimal("0"),
                    price=line.price,
                    sum=amount,
                    sort_order=idx,
                )
            )
            mirror.append(
                {
                    "product_id": line.product_guid,
                    "article": line.article,
                    "name": line.name,
                    "quantity": str(qty if qty is not None else 0),
                    "amount": str(amount),
                }
            )
    ret.line_items = mirror

    remaining_after = await sync_barter_loan_status(session, loan)
    if write_off_remainder and remaining_after > 0:
        # Недовес прощён: хвост уходит в зачётную стоимость, заём закрывается штатно, его
        # кредиторка не остаётся огрызком. Копим (+=), а не присваиваем: списывать могли и
        # по частям, при нескольких недовозвратах подряд.
        loan.barter_writeoff_amount = _money(loan.barter_writeoff_amount) + remaining_after
        await session.flush()
        await sync_barter_loan_status(session, loan)
    await session.commit()
    await session.refresh(ret)
    return ret
