"""Сторож оплат в iiko: пропажа поставленной оплаты и пара исправления чека не по схеме.

Повод — 13.10.2026, «Местный закуп»: в бэк-офисе iiko сняли оплаты чеков Ч-116 и Ч-123,
которые Тепло поставило и сверка подтвердила ещё в августе — сверка их больше не смотрела, и
пропажу не заметил никто. А «(испр.)» тех же чеков, которым по схеме исправления оплата не
положена, стояли оплаченными. Сеть (OLAP и Cloud get) подменена своей iiko — проверяем правила
сторожа: что считается пропажей, что — нормой, и как живёт случай.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cp_helpers import admin_headers, make_counterparty, make_invoice
from sqlalchemy import select

from app.models import IikoInvoicePaymentPush, ReconciliationCase
from app.services.counterparty_iiko_payment import sweep_unsettled_iiko_payments
from app.services.iiko_payment_verify import VERIFY_MAX_AGE
from app.services.iiko_payment_watch import (
    CASE_KIND,
    DEV_CORRECTION_DELETED,
    DEV_CORRECTION_PAID,
    DEV_ORIGINAL_PAYMENT_REMOVED,
    DEV_PAYMENT_REMOVED,
    DEV_RETURN_MISSING,
    LEDGER_LOOKBACK,
    REASON_CORRECTION_PAIR,
    REASON_PAYMENT_REMOVED,
    IikoDocument,
    IikoWatchUnavailable,
    LedgerPayment,
    watch_iiko_payments,
)

SUPPLIER_GUID = "SUP-LOCAL"
VICTORIA = "user-victoria"


@dataclass
class FakeIiko:
    """Своя iiko: проводки оплат, документы и пользователи — только то, что читает сторож."""

    payments: list[LedgerPayment] = field(default_factory=list)
    incoming_docs: dict[str, IikoDocument] = field(default_factory=dict)
    returned_docs: dict[str, IikoDocument] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=lambda: {VICTORIA: "Виктория Менеджер"})
    broken: set[str] = field(default_factory=set)  # документы, на которых iiko «молчит»
    ledger_calls: list[tuple[date, date]] = field(default_factory=list)
    incoming_calls: list[str] = field(default_factory=list)

    def ledger(self, date_from: date, date_to: date) -> list[LedgerPayment]:
        self.ledger_calls.append((date_from, date_to))
        return list(self.payments)

    def incoming(self, document_id: str) -> IikoDocument:
        self.incoming_calls.append(document_id)
        if document_id in self.broken:
            raise IikoWatchUnavailable("iiko Cloud: HTTP 503")
        return self.incoming_docs.get(document_id, IikoDocument(document_id, found=False))

    def returned(self, document_id: str) -> IikoDocument:
        if document_id in self.broken:
            raise IikoWatchUnavailable("iiko Cloud: HTTP 503")
        return self.returned_docs.get(document_id, IikoDocument(document_id, found=False))

    def user_name(self, user_id: str) -> str | None:
        return self.names.get(user_id)


def _doc(
    document_id, number, *, status="PROCESSED", modified_at=None, by=None, links_to=None, total=None
):
    return IikoDocument(
        document_id=document_id,
        found=True,
        number=number,
        status=status,
        counteragent_id=SUPPLIER_GUID,
        modified_at=modified_at,
        modified_by_id=by,
        incoming_invoice_id=links_to,
        total=Decimal(total) if total is not None else None,
    )


def _paid(number, amount, *, counteragent=SUPPLIER_GUID, paid_on=date(2026, 8, 30)):
    return LedgerPayment(
        number=number,
        counteragent_id=counteragent,
        account="Денежные средства, эквайринг",
        amount=Decimal(amount),
        paid_on=paid_on,
    )


async def _cheque(session, *, number, external_id, amount="2583.56", cp=None, invoice_date=None):
    """Оплаченный чек Кассы, выгруженный в iiko, — как Ч-116 до исправления."""
    if cp is None:
        cp = await make_counterparty(
            session, name=f"Местный закуп {number}", iiko_guid=SUPPLIER_GUID
        )
    inv = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount=amount,
        number=number,
        payment_status="paid",
        source="kassa_cheque",
        external_id=external_id,
        invoice_date=invoice_date or date(2026, 8, 30),
    )
    await session.flush()
    return cp, inv


async def _push(
    session, inv, *, external_id, amount, verified=True, created_age=timedelta(days=40)
):
    """Оплата, которую Тепло поставило в iiko (``add_payment`` → ok), с её подтверждением."""
    sent_at = datetime.now(UTC) - created_age
    row = IikoInvoicePaymentPush(
        idempotency_key=f"kassa_goods:{inv.id}:card:{uuid.uuid4().hex[:6]}",
        invoice_id=inv.id,
        external_id=external_id,
        amount=Decimal(amount),
        account_to="ACQ",
        status="ok",
        attempts=1,
        iiko_document_id=external_id,
        verified_at=sent_at + timedelta(minutes=40) if verified else None,
    )
    session.add(row)
    await session.flush()
    row.created_at = sent_at
    await session.commit()
    return row


async def _drift_cases(session_factory, status=None):
    async with session_factory() as session:
        query = select(ReconciliationCase).where(ReconciliationCase.kind == CASE_KIND)
        if status is not None:
            query = query.where(ReconciliationCase.status == status)
        return (await session.scalars(query.order_by(ReconciliationCase.created_at))).all()


# --- Правило 1: оплату, поставленную Тепло, сняли в iiko -------------------------------------


async def test_removed_payment_opens_case_with_document_amount_and_last_editor(
    async_session_factory,
):
    """Ч-116: оплату подтвердили давно (вне окна сверки), сейчас её в iiko нет → случай.

    В случае: документ, сумма, когда оплату видели последний раз, кто и когда правил документ
    (правка позже «видели» — правдоподобный момент снятия)."""
    edited = datetime(2026, 10, 13, 12, 1, 23, tzinfo=UTC)
    iiko = FakeIiko(incoming_docs={"X116": _doc("X116", "Ч-116", modified_at=edited, by=VICTORIA)})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-116", external_id="X116")
        push = await _push(session, inv, external_id="X116", amount="2583.56")
        result = await watch_iiko_payments(session, source=iiko)

    assert result["opened"] == 1
    [case] = await _drift_cases(async_session_factory)
    assert case.status == "pending"
    assert case.payload["reason_code"] == REASON_PAYMENT_REMOVED
    assert case.payload["invoice_number"] == "Ч-116"
    assert case.payload["amount"] == "2583.56"
    assert case.payload["retriable"] is False  # повтор — решение владельца, кнопки нет
    [dev] = case.payload["deviations"]
    assert dev["code"] == DEV_PAYMENT_REMOVED
    assert dev["document_id"] == "X116"
    assert dev["confirmed"] is True
    assert dev["last_seen_at"] == push.verified_at.isoformat()
    assert dev["modified_by"] == "Виктория Менеджер"
    assert dev["modified_after_seen"] is True
    assert dev["detected_at"]
    assert "Ч-116" in dev["text"] and "2 583,56 ₽" in dev["text"]
    assert "Виктория Менеджер" in dev["text"] and "13.10.2026 15:01" in dev["text"]
    # Сверка подтверждала долю, а не сумму документа целиком, — так и говорим.
    assert "сверка подтверждала" in dev["text"]


async def test_removal_after_watch_saw_payment_names_the_window(async_session_factory):
    """Сторож видел оплату целиком, следующим проходом её нет — случай называет окно «была —
    нет» по отметке сторожа, а не по давнему подтверждению сверки."""
    iiko = FakeIiko(
        payments=[_paid("Ч-212", "10.00")], incoming_docs={"X212": _doc("X212", "Ч-212")}
    )
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-212", external_id="X212", amount="10.00")
        push = await _push(session, inv, external_id="X212", amount="10.00")
        await watch_iiko_payments(session, source=iiko)
        await session.refresh(push)
        seen_at = push.iiko_seen_at
        assert seen_at is not None

        iiko.payments.clear()
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["last_seen_at"] == seen_at.isoformat()
    assert "целиком видели" in dev["text"]


async def test_document_paid_in_full_after_it_was_reduced_is_not_drift(async_session_factory):
    """ИП Карпов, накладная 18: Тепло ставило 18 477,13, накладную уменьшили до 18 476,08, и в
    iiko оплату переставили ровно на новую сумму. Документ оплачен целиком — пропажи нет."""
    iiko = FakeIiko(
        payments=[_paid("18", "18476.08")],
        incoming_docs={"X18": _doc("X18", "18", total="18476.08")},
    )
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="18", external_id="X18", amount="18476.08")
        push = await _push(session, inv, external_id="X18", amount="18477.13")
        result = await watch_iiko_payments(session, source=iiko)
        await session.refresh(push)

    assert result["opened"] == 0
    assert push.iiko_seen_at is not None


async def test_payment_in_place_is_marked_seen_and_window_is_wide(async_session_factory):
    """Оплата на месте — случая нет, у строки отметка «видели». Проводки читаются с запасом в
    год назад: оплату в iiko передвигают на любую дату (Ч-116 (испр.) нашли на 14.08)."""
    iiko = FakeIiko(payments=[_paid("Ч-200", "1000.00", paid_on=date(2026, 1, 5))])
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-200", external_id="X200", amount="1000.00")
        push = await _push(session, inv, external_id="X200", amount="1000.00")
        result = await watch_iiko_payments(session, source=iiko)
        await session.refresh(push)

    assert result["opened"] == 0
    assert push.iiko_seen_at is not None
    assert iiko.incoming_calls == []  # покрыто сразу — Cloud не дёргаем
    date_from, _ = iiko.ledger_calls[0]
    assert date_from <= date(2026, 8, 30) - LEDGER_LOOKBACK
    assert await _drift_cases(async_session_factory) == []


async def test_payment_found_under_number_from_iiko_document(async_session_factory):
    """Номер в нашей базе разошёлся с номером документа iiko — судим по документу, а не по
    своей записи: оплата под номером iiko на месте, случая нет."""
    iiko = FakeIiko(
        payments=[_paid("Ч-201/1", "500.00")],
        incoming_docs={"X201": _doc("X201", "Ч-201/1")},
    )
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-201", external_id="X201", amount="500.00")
        push = await _push(session, inv, external_id="X201", amount="500.00")
        result = await watch_iiko_payments(session, source=iiko)
        await session.refresh(push)

    assert result["opened"] == 0
    assert push.iiko_seen_at is not None


async def test_same_number_of_other_supplier_does_not_hide_removal(async_session_factory):
    """Номера накладных у поставщиков повторяются: чужая оплата «№ 15» не прикрывает нашу."""
    iiko = FakeIiko(
        payments=[_paid("15", "700.00", counteragent="OTHER-SUPPLIER")],
        incoming_docs={"X15": _doc("X15", "15")},
    )
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="15", external_id="X15", amount="700.00")
        await _push(session, inv, external_id="X15", amount="700.00")
        result = await watch_iiko_payments(session, source=iiko)

    assert result["opened"] == 1


async def test_partially_removed_payment_names_shortfall(async_session_factory):
    """Сняли одну из долей (карта + наличные) — случай называет, сколько не хватает."""
    iiko = FakeIiko(
        payments=[_paid("Ч-202", "300.00")], incoming_docs={"X202": _doc("X202", "Ч-202")}
    )
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-202", external_id="X202", amount="1000.00")
        await _push(session, inv, external_id="X202", amount="700.00")
        await _push(session, inv, external_id="X202", amount="300.00")
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert (dev["expected"], dev["found"], dev["amount"]) == ("1000.00", "300.00", "700.00")
    assert case.payload["amount"] == "700.00"


async def test_old_never_confirmed_payment_is_reported_as_not_found(async_session_factory):
    """Отправка старше окна сверки, которую так и не подтвердили, — тоже видна, но словами
    «подтверждения не было», а не «пропала»: что она когда-то стояла в iiko, мы не знаем."""
    iiko = FakeIiko(incoming_docs={"X203": _doc("X203", "Ч-203")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-203", external_id="X203", amount="10.00")
        await _push(
            session,
            inv,
            external_id="X203",
            amount="10.00",
            verified=False,
            created_age=VERIFY_MAX_AGE + timedelta(days=2),
        )
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["confirmed"] is False and dev["last_seen_at"] is None
    assert "Подтверждения проводки не было" in dev["text"]


async def test_fresh_unconfirmed_payment_is_left_to_verify(async_session_factory):
    """Свежую неподтверждённую отправку ведёт сверка (она сама переотправит) — сторож молчит."""
    iiko = FakeIiko()
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-204", external_id="X204", amount="10.00")
        await _push(
            session,
            inv,
            external_id="X204",
            amount="10.00",
            verified=False,
            created_age=timedelta(days=2),
        )
        result = await watch_iiko_payments(session, source=iiko)

    assert result["checked_payments"] == 0
    assert iiko.ledger_calls == []  # судить нечего — в iiko не ходим вовсе
    assert await _drift_cases(async_session_factory) == []


async def test_payment_verify_gave_up_on_is_watched_at_once(async_session_factory):
    """Сверка исчерпала переотправки — дальше строку не смотрит, а её ручной случай закрывает
    следующая же отправка зеркала (515321, 06.10.2026: случай прожил пять минут). Сторож берёт
    такую строку сразу, без ожидания окна сверки, и его случай отправка зеркала не закрывает."""
    iiko = FakeIiko(incoming_docs={"X321": _doc("X321", "515321")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="515321", external_id="X321", amount="2950.00")
        push = await _push(
            session,
            inv,
            external_id="X321",
            amount="2950.00",
            verified=False,
            created_age=timedelta(hours=5),
        )
        push.resend_count = 2
        await session.commit()
        result = await watch_iiko_payments(session, source=iiko)

    assert result["opened"] == 1
    [case] = await _drift_cases(async_session_factory)
    assert "Подтверждения проводки не было" in case.payload["deviations"][0]["text"]


async def test_rolled_back_or_manual_payments_are_not_judged(async_session_factory):
    """Оплату у нас откатили — её отсутствие в iiko норма. Ручное подтверждение — не оплата
    Тепло: сумму его Тепло не ставило, судить нечего."""
    iiko = FakeIiko()
    async with async_session_factory() as session:
        cp, unpaid = await _cheque(session, number="Ч-205", external_id="X205", amount="10.00")
        unpaid.payment_status = "unpaid"
        await _push(session, unpaid, external_id="X205", amount="10.00")
        _, manual = await _cheque(
            session, number="Ч-206", external_id="X206", amount="10.00", cp=cp
        )
        row = await _push(session, manual, external_id="X206", amount="10.00")
        row.account_to = "manual"
        await session.commit()
        result = await watch_iiko_payments(session, source=iiko)

    assert result["checked_payments"] == 0
    assert await _drift_cases(async_session_factory) == []


async def test_iiko_silence_is_not_taken_for_removal(async_session_factory):
    """Cloud не ответил по документу — случай не заводим: молчание iiko — не «оплаты нет»."""
    iiko = FakeIiko(broken={"X207"})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-207", external_id="X207", amount="10.00")
        await _push(session, inv, external_id="X207", amount="10.00")
        result = await watch_iiko_payments(session, source=iiko)

    assert result["unavailable"] == 1
    assert await _drift_cases(async_session_factory) == []


async def test_case_lives_until_payment_returns_and_keeps_first_detection(
    async_session_factory,
):
    """Повторный проход не плодит случай и помнит, когда пропажу заметили впервые; вернули
    оплату в iiko — случай закрывается сам."""
    iiko = FakeIiko(incoming_docs={"X208": _doc("X208", "Ч-208")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-208", external_id="X208", amount="10.00")
        await _push(session, inv, external_id="X208", amount="10.00")
        await watch_iiko_payments(session, source=iiko)
        [first] = await _drift_cases(async_session_factory)
        detected = first.payload["deviations"][0]["detected_at"]

        second = await watch_iiko_payments(session, source=iiko)
        assert (second["opened"], second["updated"]) == (0, 1)
        [same] = await _drift_cases(async_session_factory)
        assert same.id == first.id
        assert same.payload["deviations"][0]["detected_at"] == detected

        iiko.payments.append(_paid("Ч-208", "10.00"))
        third = await watch_iiko_payments(session, source=iiko)

    assert third["resolved"] == 1
    [closed] = await _drift_cases(async_session_factory)
    assert closed.status == "resolved"
    assert closed.resolution_payload == {"reason": "iiko_matches_again"}


async def test_dismissed_picture_stays_silent_new_picture_opens(async_session_factory):
    """«Разобрано» гасит именно эту картину: та же — не всплывает, новая — открывает случай."""
    iiko = FakeIiko(incoming_docs={"X209": _doc("X209", "Ч-209")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-209", external_id="X209", amount="10.00")
        await _push(session, inv, external_id="X209", amount="10.00")
        await watch_iiko_payments(session, source=iiko)
        [case] = await _drift_cases(async_session_factory)
        stored = await session.get(ReconciliationCase, case.id)
        stored.status = "dismissed"
        await session.commit()

        again = await watch_iiko_payments(session, source=iiko)
        assert again["silenced"] == 1
        assert len(await _drift_cases(async_session_factory, status="pending")) == 0

        # Новая картина: Тепло поставило ещё одну оплату на другой документ — и её тоже нет.
        await _push(session, inv, external_id="X209-B", amount="5.00")
        iiko.incoming_docs["X209-B"] = _doc("X209-B", "Ч-209")
        changed = await watch_iiko_payments(session, source=iiko)

    assert changed["opened"] == 1


# --- Правило 2: пара исправления чека не по схеме --------------------------------------------


async def _corrected_cheque(session, *, number="Ч-116", amount="2583.56", with_push=True):
    """Исправленный оплаченный чек: X оплачена Тепло, возврат R(X), новая «X (испр.)» — Y."""
    _, inv = await _cheque(session, number=number, external_id=f"Y-{number}", amount=amount)
    inv.iiko_correction_new_external_id = f"Y-{number}"
    inv.iiko_return_external_id = f"R-{number}"
    inv.iiko_return_status = "booked"
    await session.flush()
    if with_push:
        await _push(session, inv, external_id=f"X-{number}", amount=amount)
    else:
        await session.commit()
    return inv


def _pair_iiko(number="Ч-116", *, x_paid="2583.56", y_paid=None, return_doc=..., y_doc=...):
    iiko = FakeIiko()
    iiko.incoming_docs[f"X-{number}"] = _doc(f"X-{number}", number)
    iiko.incoming_docs[f"Y-{number}"] = (
        _doc(f"Y-{number}", f"{number} (испр.)") if y_doc is ... else y_doc
    )
    iiko.returned_docs[f"R-{number}"] = (
        _doc(f"R-{number}", "0005", links_to=f"X-{number}") if return_doc is ... else return_doc
    )
    if x_paid:
        iiko.payments.append(_paid(number, x_paid))
    if y_paid:
        iiko.payments.append(_paid(f"{number} (испр.)", y_paid, paid_on=date(2026, 8, 14)))
    return iiko


async def test_pair_by_scheme_is_clean(async_session_factory):
    """Ч-117 сейчас: X оплачена, возврат проведён и ссылается на X, «(испр.)» без оплаты."""
    iiko = _pair_iiko("Ч-117", x_paid="3253.91")
    async with async_session_factory() as session:
        inv = await _corrected_cheque(session, number="Ч-117", amount="3253.91")
        result = await watch_iiko_payments(session, source=iiko)
        push = await session.scalar(
            select(IikoInvoicePaymentPush).where(IikoInvoicePaymentPush.invoice_id == inv.id)
        )

    assert result["checked_pairs"] == 1 and result["opened"] == 0
    assert push.iiko_seen_at is not None
    assert await _drift_cases(async_session_factory) == []


async def test_pair_flags_removed_original_and_paid_correction(async_session_factory):
    """Ч-116 на 23.10: оплату исходной сняли, а «(испр.)» стоит оплаченной — один случай на
    пару, две строки. Отдельного случая «оплату сняли» по той же накладной нет."""
    iiko = _pair_iiko(x_paid=None, y_paid="2223.59")
    async with async_session_factory() as session:
        await _corrected_cheque(session)
        result = await watch_iiko_payments(session, source=iiko)

    assert result["opened"] == 1
    [case] = await _drift_cases(async_session_factory)
    assert case.payload["reason_code"] == REASON_CORRECTION_PAIR
    codes = {d["code"]: d for d in case.payload["deviations"]}
    assert set(codes) == {DEV_ORIGINAL_PAYMENT_REMOVED, DEV_CORRECTION_PAID}
    assert codes[DEV_ORIGINAL_PAYMENT_REMOVED]["document_id"] == "X-Ч-116"
    assert codes[DEV_ORIGINAL_PAYMENT_REMOVED]["amount"] == "2583.56"
    paid = codes[DEV_CORRECTION_PAID]
    assert paid["document_number"] == "Ч-116 (испр.)" and paid["amount"] == "2223.59"
    assert "14.08.2026" in paid["text"]
    assert paid["original_paid"] is False and "Оплата переехала" in paid["text"]
    assert case.payload["amount"] == "2583.56"


async def test_pair_flags_double_payment(async_session_factory):
    """Состояние «Местного закупа» на 06.10.2026: X оплачена, возврат проведён, и «(испр.)»
    тоже оплатили — оплата задвоилась. Одна строка, и она прямо называет задвоение."""
    iiko = _pair_iiko("Ч-117", x_paid="3253.91", y_paid="2968.94")
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-117", amount="3253.91")
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["code"] == DEV_CORRECTION_PAID and dev["original_paid"] is True
    assert "Оплата задвоилась" in dev["text"] and "3 253,91 ₽" in dev["text"]


@pytest.mark.parametrize(
    ("return_doc", "words"),
    [
        (IikoDocument("R-Ч-123", found=False), "не найден"),
        (_doc("R-Ч-123", "0003", status="DELETED", links_to="X-Ч-123"), "удалён"),
        (_doc("R-Ч-123", "0003", status="NEW", links_to="X-Ч-123"), "не проведён"),
        (_doc("R-Ч-123", "0003", links_to="X-OTHER"), "не на исходную"),
    ],
)
async def test_pair_flags_missing_return(async_session_factory, return_doc, words):
    """Возврата нет в любом виде: не найден, удалён, распроведён, ссылается не на X."""
    iiko = _pair_iiko("Ч-123", x_paid="609.88", return_doc=return_doc)
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-123", amount="609.88")
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["code"] == DEV_RETURN_MISSING
    assert words in dev["text"]


async def test_pair_without_return_id_is_flagged(async_session_factory):
    """Возврат не заведён вовсе — «(испр.)» стоит приходом рядом с исходной."""
    iiko = _pair_iiko("Ч-124", x_paid="100.00")
    async with async_session_factory() as session:
        inv = await _corrected_cheque(session, number="Ч-124", amount="100.00")
        inv.iiko_return_external_id = None
        await session.commit()
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    assert [d["code"] for d in case.payload["deviations"]] == [DEV_RETURN_MISSING]


async def test_pair_flags_deleted_correction(async_session_factory):
    """«(испр.)» удалили в iiko — правильная поставка больше не приходована."""
    iiko = _pair_iiko(
        "Ч-125", x_paid="100.00", y_doc=_doc("Y-Ч-125", "Ч-125 (испр.)", status="DELETED")
    )
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-125", amount="100.00")
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    assert [d["code"] for d in case.payload["deviations"]] == [DEV_CORRECTION_DELETED]


async def test_pair_without_teplo_payment_does_not_claim_removal(async_session_factory):
    """Тепло не ставило оплату исходной — это «оплата оригинала так и не дошла» (дозор
    sweep_unsettled), а не пропажа: строки «оплату сняли» быть не должно."""
    iiko = _pair_iiko("Ч-126", x_paid=None)
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-126", amount="100.00", with_push=False)
        result = await watch_iiko_payments(session, source=iiko)

    assert result["opened"] == 0


async def test_paid_correction_without_teplo_payment_on_original_is_not_called_moved(
    async_session_factory,
):
    """Тепло оплату исходной не ставило, а «(испр.)» оплачена — видно, но без домысла
    «переехала»: что оплата когда-то стояла на исходной, мы не знаем (АЛЬЯНС ЮГ)."""
    iiko = _pair_iiko("Ч-127", x_paid=None, y_paid="50.00")
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-127", amount="100.00", with_push=False)
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["code"] == DEV_CORRECTION_PAID
    assert "переехала" not in dev["text"] and "оплаты нет" in dev["text"]


async def test_paid_correction_next_to_deleted_original_says_so(async_session_factory):
    """АЛЬЯНС ЮГ и Ч-46: исходную удалили в iiko, жива одна оплаченная «(испр.)»."""
    iiko = _pair_iiko(
        "Ч-128",
        x_paid=None,
        y_paid="50.00",
        return_doc=_doc("R-Ч-128", "0001", links_to="X-Ч-128"),
    )
    iiko.incoming_docs["X-Ч-128"] = _doc("X-Ч-128", "Ч-128", status="DELETED")
    async with async_session_factory() as session:
        await _corrected_cheque(session, number="Ч-128", amount="100.00", with_push=False)
        await watch_iiko_payments(session, source=iiko)

    [case] = await _drift_cases(async_session_factory)
    [dev] = case.payload["deviations"]
    assert dev["text"].startswith("Исходная накладная Ч-128 удалена в iiko")


async def test_sweep_does_not_close_drift_case(async_session_factory):
    """Дозор «оплата не дошла» закрывает свои случаи по ok-строкам журнала. Случай сторожа он
    трогать не должен: ok-строка как раз и есть то, что разошлось с iiko."""
    iiko = FakeIiko(incoming_docs={"X210": _doc("X210", "Ч-210")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-210", external_id="X210", amount="10.00")
        await _push(session, inv, external_id="X210", amount="10.00")
        await watch_iiko_payments(session, source=iiko)
        await sweep_unsettled_iiko_payments(session)

    [case] = await _drift_cases(async_session_factory)
    assert case.status == "pending"


async def test_owner_review_lists_drift_case(client, async_session_factory):
    """Случай виден человеку в очереди «На разбор» и в фильтре своего вида."""
    iiko = FakeIiko(incoming_docs={"X211": _doc("X211", "Ч-211")})
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="Ч-211", external_id="X211", amount="10.00")
        await _push(session, inv, external_id="X211", amount="10.00")
        await watch_iiko_payments(session, source=iiko)

    headers = await admin_headers(async_session_factory)
    response = client.get("/api/v1/dds/owner-review?kind=iiko_payment_drift", headers=headers)
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["kind"] == CASE_KIND
    assert item["payload"]["invoice_number"] == "Ч-211"


def test_live_source_retries_cloud_read_on_rate_limit(monkeypatch):
    """429 на чтении документа — повод повторить, а не «документа нет»: get идемпотентен.
    Без повтора холостой прогон 23.10.2026 не дочитал две накладные ИП Карпова."""
    import app.services.iiko_cloud_client as cloud
    import app.services.iiko_payment_watch as watch

    answers = [(429, {"message": "too many requests"}), (200, {"number": "18", "items": []})]
    monkeypatch.setattr(cloud, "iiko_opener", lambda: object())
    monkeypatch.setattr(cloud, "iiko_auth_token", lambda opener: "token")
    monkeypatch.setattr(cloud, "iiko_cloud_call", lambda *args, **kwargs: answers.pop(0))
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)

    document = watch.LiveIikoWatchSource().incoming("X18")

    assert document.found and document.number == "18"
    assert answers == []


def test_live_source_reports_persistent_rate_limit_as_unavailable(monkeypatch):
    """iiko так и не ответила — судить нельзя: IikoWatchUnavailable, а не «документ удалён»."""
    import app.services.iiko_cloud_client as cloud
    import app.services.iiko_payment_watch as watch

    monkeypatch.setattr(cloud, "iiko_opener", lambda: object())
    monkeypatch.setattr(cloud, "iiko_auth_token", lambda opener: "token")
    monkeypatch.setattr(
        cloud, "iiko_cloud_call", lambda *args, **kwargs: (429, {"message": "too many requests"})
    )
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)

    with pytest.raises(IikoWatchUnavailable):
        watch.LiveIikoWatchSource().incoming("X18")


async def test_case_closes_when_payment_rolled_back_or_cheque_corrected(async_session_factory):
    """Сторожить стало нечего — случай не висит вечно. Оплату у нас откатили: отсутствие в iiko
    теперь норма. Чек исправили: дальше его судит правило пары, старый случай закрыт."""
    iiko = FakeIiko(incoming_docs={"X213": _doc("X213", "Ч-213"), "X214": _doc("X214", "Ч-214")})
    async with async_session_factory() as session:
        cp, rolled = await _cheque(session, number="Ч-213", external_id="X213", amount="10.00")
        await _push(session, rolled, external_id="X213", amount="10.00")
        _, fixed = await _cheque(session, number="Ч-214", external_id="X214", amount="10.00", cp=cp)
        await _push(session, fixed, external_id="X214", amount="10.00")
        await watch_iiko_payments(session, source=iiko)
        assert len(await _drift_cases(async_session_factory, status="pending")) == 2

        rolled.payment_status = "unpaid"
        fixed.iiko_correction_new_external_id = "Y214"
        fixed.iiko_return_external_id = "R214"
        fixed.external_id = "Y214"
        iiko.payments.append(_paid("Ч-214", "10.00"))
        iiko.incoming_docs["Y214"] = _doc("Y214", "Ч-214 (испр.)")
        iiko.returned_docs["R214"] = _doc("R214", "0006", links_to="X214")
        await session.commit()
        result = await watch_iiko_payments(session, source=iiko)

    assert result["resolved"] == 2
    reasons = {
        c.payload["invoice_number"]: c.resolution_payload["reason"]
        for c in await _drift_cases(async_session_factory)
    }
    assert reasons == {"Ч-213": "no_longer_watched", "Ч-214": "rule_changed"}


async def test_found_payment_closes_case_verify_gave_up_on(async_session_factory):
    """Сверка сдалась и завела «проводка не появляется»; человек провёл оплату в бэк-офисе —
    сторож её видит и закрывает случай своим доказательством, а не следующей отправкой."""
    from app.services.counterparty_iiko_payment import _open_iiko_payment_case
    from app.services.iiko_payment_verify import GAVE_UP_RESEND_COUNT

    iiko = FakeIiko(payments=[_paid("515321", "2950.00")])
    async with async_session_factory() as session:
        _, inv = await _cheque(session, number="515321", external_id="X321", amount="2950.00")
        push = await _push(
            session,
            inv,
            external_id="X321",
            amount="2950.00",
            verified=False,
            created_age=timedelta(hours=6),
        )
        push.resend_count = GAVE_UP_RESEND_COUNT
        await session.commit()
        await _open_iiko_payment_case(
            session,
            invoice_id=inv.id,
            external_id="X321",
            amount=Decimal("2950.00"),
            reason="проводка не появляется",
            reason_code="payment_not_in_iiko_ledger",
        )
        result = await watch_iiko_payments(session, source=iiko)
        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.kind == "iiko_payment_unsettled")
        )

    assert result["resolved"] == 1
    assert case.status == "resolved"
    assert case.resolution_payload == {"reason": "found_in_iiko_ledger"}
