"""Оплата исправленной накладной без второй траты денег — шаги 3 и 4 цепочки исправления.

Решение владельца 23.10.2026: «Происходит таким образом… в первую очередь делает сторно на
исправленную накладную ровно на ту же сумму… эта оплата обнуляется, склад актуализируется, и
после этого создаётся ещё одна уже исправленная конечная накладная, и на неё тоже накладывается
оплата… Никто руками ничего не делает». И раньше, про чек: деньги уплачены один раз, исправление
меняет только структуру чека — часть товара на деле питание персонала, и она должна стать
расходом своей статьи, а не долгом поставщика.

Цепочка (``warehouse_invoice_push.book_correction_in_iiko``):

1. **Сторно** исходной X — возвратная накладная iiko со ссылкой на X ровно на сумму X (строки
   берутся из самой X в iiko). Проводки: склад → «Прочие доходы» по себестоимости, «Прочие
   доходы» → «Задолженность перед поставщиками»[поставщик] на сумму X. Оплата X «обнуляется»
   в кредит: поставщик должен нам всю уплаченную сумму.
2. **Новая «X (испр.)»** на правильные товары: «Задолженность»[поставщик] → склад на сумму Y.
3. **Оплата «(испр.)» зачётом** (здесь): ``add_payment`` на Y со счёта «Авансы поставщиков» на
   min(X, Y) — тот же кредит, что дало сторно, без денег. Проба 25.10.2026 на 3 ₽ (КВАРЦ)
   прочитала проводку: «Авансы поставщиков»[поставщик] → «Задолженность»[поставщик] — контрагент
   стоит на обеих сторонах, документ оплачен, общий счёт с поставщиком не сдвинулся (там же
   «Задолженность» +2, «Авансы» −2). Счёт «Задолженность перед поставщиками» для зачёта не
   годится: проба 11.07.2026 сдвинула им баланс поставщика на всю сумму накладной. Сразу после
   проведения «(испр.)» iiko отвечает «already paid» и ничего не проводит (та же проба: через
   2 секунды — отказ, через минуты — 201), поэтому зачёт ставит фоновый проход из очереди
   (``iiko_correction_pay_status = pending``), не раньше ``OFFSET_SETTLE_DELAY`` после проведения,
   а после 201 читает проводку по GUID документа.
4. **Разница чека — расходом своей статьи** (здесь): остаток кредита X − Y у чека Кассы — это
   строки, которые исправление перенесло из товара в расход (питание персонала и т. п.).
   Изъятием его не провести: деньги уже ушли оплатой X, а изъятие списало бы их второй раз; типа
   изъятия «со счёта поставщика» в iiko нет, внесения (``addPayIn``) нет. Проводит его акт
   приёма услуг от того же поставщика (``/api/finance/v1/incoming_service``): строка на счёт
   статьи («Затраты на персонал») — «Задолженность»[поставщик] закрывается расходом (проба
   25.10.2026: «Задолженность»[поставщик] → «Затраты на персонал» 1 ₽). Нужна
   номенклатура-услуга (``iiko_correction_service_product_id``), её заводят один раз.

Деньги не трогаются ни на одном шаге: оплата X с эквайринга и изъятия по расходным статьям чека
остаются, как были, и вместе по-прежнему равны оплате чека. Банковская накладная (не чек)
шага 4 не имеет: её разница — настоящая переплата, она и остаётся кредитом поставщика.
"""

from __future__ import annotations

import json
import logging
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChequeIikoPayout,
    DdsArticle,
    IikoInvoicePaymentPush,
    InvoiceLineItem,
    SupplierInvoice,
)
from app.services.counterparty_iiko_payment import _is_already_paid
from app.services.iiko_payment_watch import IikoDocument, LedgerPayment

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")

# Счёт-источник зачёта. Резолвим по имени: id на dev и проде разные.
ADVANCES_ACCOUNT_NAME = "Авансы поставщиков"
# Ключ строки журнала отправок для оплаты «(испр.)» зачётом — держит идемпотентность шага 3.
OFFSET_KEY_PREFIX = "correction_offset:"
# Сколько «(испр.)» должна пролежать проведённой, прежде чем на неё ставить зачёт: проба
# 25.10.2026 — через 2 секунды после проведения iiko ответила «already paid», через минуты — 201.
OFFSET_SETTLE_DELAY = timedelta(minutes=2)
# Столько раз подряд «already paid» на зачёт — и стоп: значит, «(испр.)» правда кто-то оплатил.
OFFSET_ALREADY_PAID_LIMIT = 3
# Расхождение в копейку терпим (округление строк), больше — уже не та сумма.
KOPECK = Decimal("0.01")
_SERVICE_CREATE = "/api/finance/v1/incoming_service/create"
_SERVICE_POST = "/api/finance/v1/incoming_service/post"


@dataclass(frozen=True)
class ExpenseShare:
    """Прирост расходной статьи чека, которого нет в iiko: исправление перенесло эти деньги из
    товара в расход, а изъятие по статье проведено ещё на старую сумму."""

    article_name: str  # статья Тепла («Расходы на питание персонала»)
    account_name: str  # счёт iiko, на который ложится расход («Затраты на персонал»)
    amount: Decimal


@dataclass
class CorrectionPaymentPlan:
    """Что поставить на шагах 3–4, чтобы исправление легло без второй траты денег."""

    storno_total: Decimal  # сумма сторно, то есть кредит поставщика после шага 1
    paid_for_original: Decimal  # деньги, уплаченные за исходную X (журнал Тепла)
    correction_total: Decimal  # Y — сумма новой «(испр.)»
    offset_amount: Decimal  # оплата «(испр.)» зачётом из кредита = min(сторно, Y)
    expense_shares: list[ExpenseShare]  # разница X − Y по расходным статьям (только чек)
    leftover_credit: Decimal  # что останется кредитом поставщика (переплата банковской)
    problems: list[str] = field(default_factory=list)  # почему довести нельзя

    @property
    def expense_total(self) -> Decimal:
        return sum((share.amount for share in self.expense_shares), Decimal("0"))


def build_payment_plan(
    *,
    storno_total: Decimal,
    paid_for_original: Decimal,
    correction_total: Decimal,
    expense_shares: list[ExpenseShare],
    share_problems: list[str],
    is_cheque: bool,
) -> CorrectionPaymentPlan:
    """Разложить кредит сторно на зачёт «(испр.)» и расход разницы (решение 23.10.2026).

    - Зачёт — min(X, Y): «(испр.)» оплачивается из уже уплаченного, не больше кредита.
    - «(испр.)» дороже исходной — доплата деньгами, которой у Тепла нет основания поставить
      автоматически (за ней стоит новая трата): стоп и случай.
    - Чек: разница X − Y обязана сойтись с приростом расходных статей — это те же деньги, только
      перенесённые в расход. Не сошлась (поменяли цену, удалили строку) — стоп: угадывать, куда
      лёг остаток, нельзя. Единственный объяснимый остаток — копейки между кредитом сторно и
      деньгами, реально уплаченными за X: старая сага строила возврат по снимку цен Тепла
      (0004 = 3 254,04 при оплате Ч-117 3 253,91), а сама X в iiko могла стоить на копейки
      больше уплаченного (Ч-116: 2 583,63 при оплате 2 583,56). Он остаётся кредитом поставщика.
    - Банковская накладная: разница — переплата поставщику, она и остаётся его кредитом."""
    problems = list(share_problems)
    storno_drift = (storno_total - paid_for_original).quantize(KOPECK)
    offset = min(storno_total, correction_total)
    if correction_total - storno_total > KOPECK:
        problems.append(
            f"новая накладная ({correction_total} ₽) дороже исходной ({storno_total} ₽): доплату"
            " деньгами автоматически не ставим"
        )
    expense_total = sum((share.amount for share in expense_shares), Decimal("0"))
    leftover = (storno_total - offset - expense_total).quantize(KOPECK)
    if is_cheque and abs(leftover - storno_drift) > KOPECK:
        problems.append(
            f"разница исходной и новой ({(storno_total - offset).quantize(KOPECK)} ₽) не сходится"
            f" с приростом расходных статей чека ({expense_total.quantize(KOPECK)} ₽)"
        )
    return CorrectionPaymentPlan(
        storno_total=storno_total,
        paid_for_original=paid_for_original,
        correction_total=correction_total,
        offset_amount=offset.quantize(KOPECK),
        expense_shares=expense_shares,
        leftover_credit=leftover,
        problems=problems,
    )


async def cheque_expense_shares(
    session: AsyncSession, invoice: SupplierInvoice
) -> tuple[list[ExpenseShare], list[str]]:
    """Прирост расходных статей исправленного чека сверх уже проведённых изъятий.

    Новая сумма статьи — по строкам чека после исправления (возвраты в магазин не в счёт, как и
    при проведении чека); проведённое — по журналу изъятий ``kassa_cheque_iiko_payout``.
    Уменьшение статьи — отдельная беда: изъятие необратимо, его не откатить, — называем её."""
    from app.services.kassa.cheque_payout_push import ARTICLE_TO_IIKO, SUPPLIER_ARTICLE_NAME

    new_sums = dict(
        (
            await session.execute(
                select(DdsArticle.name, func.sum(InvoiceLineItem.sum))
                .join(DdsArticle, DdsArticle.id == InvoiceLineItem.dds_article_id)
                .where(
                    InvoiceLineItem.invoice_id == invoice.id,
                    InvoiceLineItem.is_return.is_(False),
                )
                .group_by(DdsArticle.name)
            )
        ).all()
    )
    posted = dict(
        (
            await session.execute(
                select(DdsArticle.name, func.sum(ChequeIikoPayout.amount))
                .join(DdsArticle, DdsArticle.id == ChequeIikoPayout.dds_article_id)
                .where(
                    ChequeIikoPayout.invoice_id == invoice.id,
                    ChequeIikoPayout.status == "posted",
                )
                .group_by(DdsArticle.name)
            )
        ).all()
    )
    shares: list[ExpenseShare] = []
    problems: list[str] = []
    for name in sorted(set(new_sums) | set(posted)):
        if name == SUPPLIER_ARTICLE_NAME or name not in ARTICLE_TO_IIKO:
            continue
        delta = (
            Decimal(str(new_sums.get(name) or 0)) - Decimal(str(posted.get(name) or 0))
        ).quantize(KOPECK)
        if delta > 0:
            shares.append(ExpenseShare(name, ARTICLE_TO_IIKO[name][0], delta))
        elif delta < 0:
            problems.append(
                f"статья «{name}» уменьшилась на {-delta} ₽, а изъятие по ней уже проведено —"
                " откатить его нельзя"
            )
    return shares, problems


class CorrectionIiko(Protocol):
    """Что цепочке нужно от iiko на шагах 3–4. Боевой — :class:`LiveCorrectionIiko`."""

    def incoming(self, document_id: str) -> IikoDocument: ...

    def returned(self, document_id: str) -> IikoDocument: ...

    def ledger(self, date_from: date, date_to: date) -> list[LedgerPayment]: ...

    def account_id(self, name: str) -> str | None: ...

    def create_service_act(self, body: dict) -> tuple[str | None, bool, str | None]: ...

    def post_service_act(self, document_id: str) -> tuple[bool, str | None]: ...

    def payment_legs(self, document_id: str) -> list[tuple[str | None, Decimal]]:
        """Проводки оплаты документа: (счёт-источник, сумма) — чтение по GUID."""
        ...


class LiveCorrectionIiko:
    """Живая iiko для шагов 3–4: чтение — как у сторожа оплат, запись — акт услуг Cloud."""

    def __init__(self) -> None:
        from app.services.iiko_payment_watch import LiveIikoWatchSource

        self._reader = LiveIikoWatchSource()
        self._accounts: dict[str, str] | None = None

    def incoming(self, document_id: str) -> IikoDocument:
        return self._reader.incoming(document_id)

    def returned(self, document_id: str) -> IikoDocument:
        return self._reader.returned(document_id)

    def ledger(self, date_from: date, date_to: date) -> list[LedgerPayment]:
        return self._reader.ledger(date_from, date_to)

    def account_id(self, name: str) -> str | None:
        """GUID счёта iiko по имени (план счетов iikoServer)."""
        if self._accounts is None:
            from app.services.kassa.iiko_cashshift_sync import _auth_token, _fetch_accounts_map

            self._accounts = {
                title: guid for guid, title in _fetch_accounts_map(_auth_token()).items()
            }
        return self._accounts.get(name)

    def _cloud(self, path: str, body: dict) -> tuple[int, Any]:
        from app.services.iiko_cloud_client import iiko_cloud_call

        time_module.sleep(0.7)  # Cloud режет частые запросы (429), см. iiko_payment_watch
        return iiko_cloud_call(path, body)

    def create_service_act(self, body: dict) -> tuple[str | None, bool, str | None]:
        from app.services.iiko_invoice_cloud import business_error_message, extract_document_id

        status, response = self._cloud(_SERVICE_CREATE, body)
        if not (200 <= status < 300):
            return None, False, business_error_message(response) or f"create HTTP {status}"
        document_id = extract_document_id(response)
        if not document_id:
            return None, False, "create: iiko не вернул documentId"
        posted, error = self.post_service_act(document_id)
        return document_id, posted, error

    def post_service_act(self, document_id: str) -> tuple[bool, str | None]:
        from app.services.iiko_invoice_cloud import business_error_message
        from app.services.iiko_location import get_organization_id

        status, response = self._cloud(
            _SERVICE_POST, {"organizationId": get_organization_id(), "documentId": document_id}
        )
        if 200 <= status < 300:
            return True, None
        message = business_error_message(response) or f"post HTTP {status}"
        # Повторный post уже проведённого акта — не ошибка: акт на месте.
        if "already" in json.dumps(response, ensure_ascii=False).lower():
            return True, None
        return False, message

    def payment_legs(self, document_id: str) -> list[tuple[str | None, Decimal]]:
        """Проводки ``INVOICE_PAYMENT`` документа (``/api/finance/v1/document_transactions``)."""
        from app.services.iiko_location import get_organization_id

        status, rows = self._cloud(
            "/api/finance/v1/document_transactions/list",
            {"organizationId": get_organization_id(), "documentId": document_id},
        )
        if status != 200 or not isinstance(rows, list):
            from app.services.iiko_payment_watch import IikoWatchUnavailable

            raise IikoWatchUnavailable(f"проводки документа: HTTP {status}")
        return [
            ((row.get("from") or {}).get("account"), Decimal(str(row.get("sum") or 0)))
            for row in rows
            if row.get("type") == "INVOICE_PAYMENT"
        ]


def _document_moment(invoice: SupplierInvoice) -> datetime:
    """Дата документа: проводки исправления ложатся тем же днём, что исходная."""
    if invoice.issued_at is not None:
        return invoice.issued_at
    if invoice.invoice_date is not None:
        return datetime.combine(invoice.invoice_date, time(12, 0), _MSK)
    return datetime.now(_MSK)


async def _fail(session: AsyncSession, invoice: SupplierInvoice, message: str) -> str:
    invoice.iiko_return_status = "failed"
    invoice.iiko_return_error = message[:500]
    invoice.iiko_correction_pay_status = "blocked"
    await session.commit()
    return "blocked"


async def queue_correction_payment(session: AsyncSession, invoice: SupplierInvoice) -> None:
    """Поставить шаги 3–4 в очередь фонового прохода (зовёт сага после «(испр.)»)."""
    if invoice.iiko_correction_pay_status != "done":
        invoice.iiko_correction_pay_status = "pending"
        await session.commit()


async def complete_pending_corrections(
    session: AsyncSession, *, iiko: CorrectionIiko | None = None, enabled: bool | None = None
) -> dict[str, int]:
    """Фоновый проход: довести шаги 3–4 у исправлений из очереди. Без людей и кнопок.

    Берёт только ``pending`` — то, что поставила в очередь сама сага; старые пары (до цепочки)
    и остановленные (``blocked``) не трогает: их доводят по слову человека."""
    from app.core.config import get_settings

    if not (get_settings().iiko_correction_offset_enabled if enabled is None else enabled):
        return {}
    ids = (
        await session.scalars(
            select(SupplierInvoice.id).where(
                SupplierInvoice.iiko_correction_pay_status == "pending"
            )
        )
    ).all()
    counts: dict[str, int] = {}
    for invoice_id in ids:
        outcome = await complete_correction_payment(session, invoice_id, iiko=iiko, enabled=True)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


@dataclass
class CorrectionInspection:
    """Что цепочка видит в iiko по исправленной накладной — до любой записи."""

    returned: IikoDocument | None = None  # сторно (возврат исходной)
    original: IikoDocument | None = None  # исходная X
    correction: IikoDocument | None = None  # новая «(испр.)»
    plan: CorrectionPaymentPlan | None = None
    original_paid: Decimal = Decimal("0")  # оплата исходной в учёте iiko
    paid_for_original: Decimal = Decimal("0")  # сколько денег за исходную уплачено (журнал)
    correction_paid: Decimal = Decimal("0")  # оплата «(испр.)» в учёте iiko
    blockers: list[str] = field(default_factory=list)  # почему шаги 3–4 сейчас ставить нельзя


async def inspect_correction(
    session: AsyncSession, invoice: SupplierInvoice, iiko: CorrectionIiko
) -> CorrectionInspection:
    """Прочитать сторно, исходную, «(испр.)» и их оплаты, разложить план. Только чтение.

    Блокеры — то, при чём зачёт поставил бы оплату не из тех денег: сторно или «(испр.)» не
    проведены; оплаты исходной в iiko нет (её сняли руками — кредита сторно за ней нет);
    «(испр.)» уже оплачена помимо Тепла (зачёт сверху задвоил бы оплату); план не сходится."""
    from app.services.iiko_payment_watch import _Ledger, _total

    result = CorrectionInspection()
    correction_id = invoice.iiko_correction_new_external_id or ""
    return_id = invoice.iiko_return_external_id
    if not return_id:
        result.blockers.append("Сторно исходной не проведено")
        return result
    try:
        result.returned = await anyio.to_thread.run_sync(lambda: iiko.returned(return_id))
        result.correction = await anyio.to_thread.run_sync(lambda: iiko.incoming(correction_id))
        original_id = result.returned.incoming_invoice_id or ""
        result.original = (
            await anyio.to_thread.run_sync(lambda: iiko.incoming(original_id))
            if original_id
            else None
        )
    except Exception as exc:  # noqa: BLE001 — iiko молчит: судить и писать нельзя
        result.blockers.append(f"iiko не ответила: {exc}")
        return result
    returned, correction, original = result.returned, result.correction, result.original
    if not returned.found or returned.status != "PROCESSED":
        result.blockers.append("Сторно исходной в iiko не проведено")
    if not correction.found or correction.status != "PROCESSED":
        result.blockers.append("Новая накладная в iiko не проведена")
    if original is None or not original.found:
        result.blockers.append("Исходная накладная в iiko не найдена")
    if result.blockers:
        return result

    is_cheque = invoice.source in ("kassa_cheque", "kassa_invoice")
    shares, share_problems = (
        await cheque_expense_shares(session, invoice) if is_cheque else ([], [])
    )
    # Деньги за X — по журналу отправок Тепла (ok-строки на документ X, не ручные подтверждения);
    # их нет (X платили помимо Тепла) — по сумме самой X.
    paid_for_original = await session.scalar(
        select(func.sum(IikoInvoicePaymentPush.amount)).where(
            IikoInvoicePaymentPush.invoice_id == invoice.id,
            IikoInvoicePaymentPush.external_id == original.document_id,
            IikoInvoicePaymentPush.status == "ok",
            IikoInvoicePaymentPush.account_to != "manual",
            IikoInvoicePaymentPush.amount > 0,
        )
    )
    result.paid_for_original = Decimal(str(paid_for_original or original.total or 0))
    result.plan = build_payment_plan(
        storno_total=returned.total or Decimal("0"),
        paid_for_original=result.paid_for_original,
        correction_total=correction.total or Decimal("0"),
        expense_shares=shares,
        share_problems=share_problems,
        is_cheque=is_cheque,
    )
    result.blockers.extend(result.plan.problems)

    # Оплаты — по учёту iiko, окно широкое: оплату двигают по датам («Ч-116 (испр.)» — 14.08
    # при накладной 30.08).
    around = _document_moment(invoice).date()
    try:
        ledger = _Ledger(
            await anyio.to_thread.run_sync(
                lambda: iiko.ledger(around - timedelta(days=366), around + timedelta(days=366))
            )
        )
    except Exception as exc:  # noqa: BLE001
        result.blockers.append(f"iiko не отдала проводки оплат: {exc}")
        return result
    counteragents = {correction.counteragent_id} if correction.counteragent_id else set()
    result.original_paid = _total(ledger.payments(original.number, counteragents))
    result.correction_paid = _total(ledger.payments(correction.number, counteragents))
    return result


async def complete_correction_payment(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    iiko: CorrectionIiko | None = None,
    enabled: bool | None = None,
) -> str:
    """Шаги 3–4 цепочки: оплатить «(испр.)» зачётом и провести разницу чека расходом.

    Возвращает ``disabled`` (настройка выключена — Y остаётся без оплаты, как раньше),
    ``not_queued`` (исправления нет в очереди), ``waiting`` (рано: «(испр.)» только что
    проведена или iiko ответила «already paid» — следующий проход), ``done`` или ``blocked``
    (причина — в ``iiko_return_error``). Ничего не шлёт по блокерам :func:`inspect_correction`.
    Идемпотентно: зачёт держит строка журнала ``correction_offset:<id>``, акт —
    ``iiko_correction_act_external_id``."""
    from app.core.config import get_settings
    from app.services.counterparty_iiko_payment import push_invoice_payment_to_iiko
    from app.services.iiko_invoice_cloud import format_iiko_invoice_datetime
    from app.services.iiko_location import get_organization_id

    settings = get_settings()
    if not (settings.iiko_correction_offset_enabled if enabled is None else enabled):
        return "disabled"
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None or not invoice.iiko_correction_new_external_id:
        return "disabled"
    if invoice.iiko_correction_pay_status != "pending":
        return "not_queued"
    correction_id = invoice.iiko_correction_new_external_id
    if iiko is None:
        from app.services.iiko_sync import _load_source_credential_env

        await _load_source_credential_env(session)
        iiko = LiveCorrectionIiko()
    number = invoice.number or ""
    moment = _document_moment(invoice)

    seen = await inspect_correction(session, invoice, iiko)
    if seen.plan is None or (seen.blockers and seen.plan.problems):
        return await _fail(
            session, invoice, "Оплату исправленной не ставим: " + "; ".join(seen.blockers)
        )
    plan, original, correction = seen.plan, seen.original, seen.correction
    assert original is not None and correction is not None

    # --- ШАГ 3: оплата «(испр.)» зачётом из кредита сторно ---
    offset_key = f"{OFFSET_KEY_PREFIX}{invoice.id}"
    done = await session.scalar(
        select(IikoInvoicePaymentPush.id).where(
            IikoInvoicePaymentPush.idempotency_key == offset_key,
            IikoInvoicePaymentPush.status == "ok",
        )
    )
    if done is None and plan.offset_amount > 0:
        settled_since = correction.modified_at
        if settled_since is not None and datetime.now(UTC) - settled_since < OFFSET_SETTLE_DELAY:
            return "waiting"
        if seen.blockers:
            return await _fail(
                session, invoice, "Оплату исправленной не ставим: " + "; ".join(seen.blockers)
            )
        # Зачёт — из денег, уплаченных за исходную. Если оплаты исходной в iiko нет (её сняли
        # руками), кредита сторно за ней тоже нет: «(испр.)» стала бы оплаченной из пустоты.
        if seen.original_paid < seen.paid_for_original - KOPECK:
            return await _fail(
                session,
                invoice,
                f"Оплата исходной «{original.number}» в iiko не стоит ({seen.original_paid} ₽ из"
                f" уплаченных {seen.paid_for_original} ₽) — зачёт не ставим",
            )
        # «(испр.)» уже кто-то оплатил (руками, своим счётом) — зачёт сверху задвоил бы оплату.
        if seen.correction_paid > 0:
            return await _fail(
                session,
                invoice,
                f"«{correction.number}» уже оплачена в iiko на {seen.correction_paid} ₽ помимо"
                " Тепла — зачёт не ставим, разберите задвоение",
            )
        advances = await anyio.to_thread.run_sync(lambda: iiko.account_id(ADVANCES_ACCOUNT_NAME))
        if not advances:
            return await _fail(session, invoice, f"В iiko нет счёта «{ADVANCES_ACCOUNT_NAME}»")
        res = await push_invoice_payment_to_iiko(
            session,
            external_id=correction_id,
            amount=plan.offset_amount,
            account_id=advances,
            idempotency_key=offset_key,
            payment_dt=moment,
            invoice_id=invoice.id,
        )
        invoice = await session.get(SupplierInvoice, invoice_id)
        if not res.ok:
            return await _fail(
                session, invoice, f"Зачёт оплаты исправленной не прошёл: {res.error or '—'}"
            )
        if res.status_code is not None and _is_already_paid(res.response):
            # «already paid» на «(испр.)», которую никто не оплачивал, — iiko ещё не дочитала
            # документ (проба 25.10.2026). Проводки нет: это не успех. Строку журнала — в ошибку,
            # следующий проход пошлёт заново; повтор безопасен, отказ ничего не провёл.
            row = await session.scalar(
                select(IikoInvoicePaymentPush).where(
                    IikoInvoicePaymentPush.idempotency_key == offset_key
                )
            )
            row.status = "error"
            row.error = "iiko ответила «already paid» на непроведённый зачёт — повторим"
            await session.commit()
            if row.attempts >= OFFSET_ALREADY_PAID_LIMIT:
                return await _fail(
                    session,
                    invoice,
                    f"iiko {row.attempts} раза ответила «already paid» на зачёт"
                    f" «{correction.number}» — похоже, её оплатили помимо Тепла; разберите"
                    " задвоение",
                )
            return "waiting"
        # Ответ записи не доказывает проводку: читаем её у документа по GUID.
        try:
            legs = await anyio.to_thread.run_sync(lambda: iiko.payment_legs(correction_id))
        except Exception as exc:  # noqa: BLE001
            return await _fail(session, invoice, f"Проводку зачёта прочитать не удалось: {exc}")
        # Сумма, а не одна строка: непредставимую для iiko сумму зачёт ставит долями.
        landed = sum((amount for account, amount in legs if account == advances), Decimal("0"))
        if abs(landed - plan.offset_amount) > KOPECK:
            return await _fail(
                session,
                invoice,
                f"iiko приняла зачёт «{correction.number}», но проводки со счёта"
                f" «{ADVANCES_ACCOUNT_NAME}» у документа нет — сверка проверит",
            )

    # --- ШАГ 4: разница чека — расходом своей статьи (акт услуг) ---
    if plan.expense_shares and not invoice.iiko_correction_act_external_id:
        product = settings.iiko_correction_service_product_id
        if not product:
            # Недонастройка, а не стоп: исправление остаётся в очереди и доведётся само, как только
            # услугу заведут и впишут в настройку. Причину видно в накладной.
            invoice.iiko_return_error = (
                f"Разница {plan.expense_total} ₽ ждёт акта: в iiko не заведена услуга для акта"
                " разницы (настройка iiko_correction_service_product_id)"
            )
            await session.commit()
            return "waiting"
        items = []
        for index, share in enumerate(plan.expense_shares, start=1):
            account = await anyio.to_thread.run_sync(
                lambda name=share.account_name: iiko.account_id(name)
            )
            if not account:
                return await _fail(session, invoice, f"В iiko нет счёта «{share.account_name}»")
            items.append(
                {
                    "num": index,
                    "product": product,
                    "amount": 1,
                    "price": float(share.amount),
                    "sum": float(share.amount),
                    "revenueAccount": account,
                    "vatPercent": 0,
                }
            )
        body = {
            "organizationId": get_organization_id(),
            "counteragent": correction.counteragent_id,
            "date": format_iiko_invoice_datetime(moment),
            "number": f"{number} (испр.) расход",
            "comment": (
                f"Исправление чека №{number}: часть, перенесённая из товара в расход, — из уже"
                " уплаченного, без новой оплаты"
            ),
            "items": items,
        }
        act_id, posted, error = await anyio.to_thread.run_sync(
            lambda: iiko.create_service_act(body)
        )
        if act_id:
            invoice.iiko_correction_act_external_id = act_id
            await session.commit()
        if not posted:
            return await _fail(
                session, invoice, f"Акт разницы {plan.expense_total} ₽ не проведён: {error or '—'}"
            )
    elif plan.expense_shares and invoice.iiko_return_status == "failed":
        # Ретрай: акт создан прошлым проходом, но не проведён — проводим тот же, второй не заводим.
        act_id = invoice.iiko_correction_act_external_id or ""
        posted, error = await anyio.to_thread.run_sync(lambda: iiko.post_service_act(act_id))
        if not posted:
            return await _fail(session, invoice, f"Акт разницы не проведён: {error or '—'}")

    invoice.iiko_return_status = "booked"
    invoice.iiko_return_error = None
    invoice.iiko_correction_pay_status = "done"
    await session.commit()
    logger.info(
        "исправление %s: «(испр.)» оплачена зачётом %s, расход разницы %s",
        invoice_id,
        plan.offset_amount,
        plan.expense_total,
    )
    return "done"
