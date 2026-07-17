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
    # Явная сумма строки (эталон из документа поставщика). Если задана — хранится как есть, а
    # не пересчитывается кол-во×цена: цена в 2 знака не всегда даёт точную сумму (80×41,225 =
    # 3298, но округлённая цена 41,23 даёт 3298,40). None → обратная совместимость (кол-во×цена).
    sum: Decimal | None = None


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
        # Явная сумма строки — эталон (если пришла); иначе кол-во×цена. Так итог накладной
        # совпадает с введённым в форме: округление цены до копейки не «уводит» сумму.
        line_sum = _money(line.sum) if line.sum is not None else _money(quantity * price)
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
        # Явная сумма строки — эталон (если пришла); иначе кол-во×цена. Так итог накладной
        # совпадает с введённым в форме: округление цены до копейки не «уводит» сумму.
        line_sum = _money(line.sum) if line.sum is not None else _money(quantity * price)
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
    counterparty_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Replace an invoice's lines and recompute totals. Only for UNPAID, non-barter invoices
    (paid allocations are tied to the amount; barter has its own loan/return ledger). The lines
    are rebuilt exactly like ``create_warehouse_invoice``. iiko push state is reset to
    ``not_pushed`` (so a corrected invoice can be pushed) UNLESS it was already pushed — then
    ``external_id`` is kept so the round-trip dedup still recognises it (the stale iiko document
    is the owner's to delete + re-push).

    ``counterparty_id`` (опционально) меняет поставщика накладной. Смена разрешена в тех же
    рамках, что и вся правка (unpaid / не бартер / не в банке); при зеркалировании в iiko
    (``propagate_invoice_edit_to_iiko``) новый поставщик подставится в ``counteragent`` документа —
    гейт «у нового поставщика есть iiko-GUID для уже выгруженной накладной» стоит в роуте."""
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

    # Смена поставщика: применяем до пересчёта строк, чтобы номенклатурный гард/iiko-пуш работали
    # уже с новым контрагентом. Проверяем существование (FK RESTRICT дал бы 500 позже).
    if counterparty_id is not None and counterparty_id != invoice.counterparty_id:
        new_counterparty = await session.get(Counterparty, counterparty_id)
        if new_counterparty is None:
            raise WarehouseInvoiceError("Контрагент не найден")
        invoice.counterparty_id = counterparty_id

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
        returned = await _loan_returned_amount(session, loan.id)
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
    returned_total = await _loan_returned_amount(session, loan_id)
    return {
        "id": str(loan.id),
        "number": loan.number,
        "we_lend": loan.direction == "receivable",
        "amount": float(_money(loan.amount)),
        "remaining": float(_money(loan.amount - returned_total)),
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
            }
            for line in lines
        ],
    }


async def create_barter_return(
    session: AsyncSession,
    *,
    loan_id: uuid.UUID,
    issued_at: datetime,
    returns: Sequence[ReturnLineInput],
    number: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    loan = await session.get(SupplierInvoice, loan_id)
    if loan is None or loan.barter_role != "loan":
        raise WarehouseInvoiceError("Заём не найден")
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
    returned_total = await _loan_returned_amount(session, loan_id)
    returned_qty = await _loan_line_returned_qty(session, loan_id)
    loan_remaining = _money(loan.amount - returned_total)

    # Сумма возврата ТОВАРОМ = кол-во × ИСХОДНАЯ цена займа: товар возвращается на склад
    # по той же стоимости, по которой ушёл, даже если рыночная цена изменилась. Возврат
    # ДЕНЬГАМИ (строка без товара) — переданная сумма денег как есть.
    resolved: list[tuple[InvoiceLineItem | None, Decimal | None, Decimal]] = []
    for r in items:
        line = loan_lines.get(r.loan_line_item_id) if r.loan_line_item_id else None
        if line is not None:
            if r.quantity is None:
                raise WarehouseInvoiceError(f"Укажите количество для «{line.name}»")
            qty = _qty(r.quantity)
            already = returned_qty.get(line.id, Decimal("0"))
            if _qty(already + qty) > _qty(line.quantity):
                raise WarehouseInvoiceError(f"Возврат по «{line.name}» превышает остаток")
            resolved.append((line, qty, _money(qty * line.price)))
        else:
            resolved.append((None, None, _money(r.amount)))

    new_total = _money(sum((amt for _, _, amt in resolved), Decimal("0.00")))
    if new_total <= 0:
        raise WarehouseInvoiceError("Сумма возврата должна быть больше нуля")
    if new_total > loan_remaining:
        raise WarehouseInvoiceError(f"Возврат {new_total} превышает остаток займа {loan_remaining}")

    ret = SupplierInvoice(
        counterparty_id=loan.counterparty_id,
        source="manual",
        direction="payable" if loan.direction == "receivable" else "receivable",
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

    new_returned = _money(returned_total + new_total)
    if new_returned >= _money(loan.amount):
        loan.barter_return_status = "returned"
        loan.payment_status = "paid"
    else:
        loan.barter_return_status = "partially_returned"
    await session.commit()
    await session.refresh(ret)
    return ret
