"""Снимок квитанции → строки «Страницы на оплату»: что обязано случиться и чего случиться нельзя.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. Приёмка — единственное место, где встречаются распознавание и учёт, и
ошибиться она может тихо: в очередь оплат попадёт правдоподобное, но неверное число, а увидят
это через месяц по звонку арендодателя. Поэтому проверяем стыки:

* деньги уходят ТОМУ, КОГО назвал поток, а не тому, кто напечатан в квитанции. В бумаге стоят
  реквизиты Водоканала и энергосбыта — договор с ними заключал арендодатель, и платёж по этим
  реквизитам ушёл бы постороннему юрлицу;
* у электричества расход и сумма к оплате расходятся, и в документы обязаны попасть ОБА числа:
  закрывающий на полный расход месяца, счёт — на остаток после зачёта аванса;
* авансовый акт не создаёт расхода вовсе — иначе месяц признается дважды: сначала авансом,
  потом фактом;
* пересняли ту же бумагу — второго долга не появляется. Дедуп по файлу этого не ловит: байты
  другие, а месяц тот же;
* разобрать не удалось — строка всё равно есть. Молча потерянный документ хуже неверного:
  о нём не узнают вообще.

OCR подменён фикстурами настоящих актов ИП Гордеева: проверяем учёт, а не зрение модели —
у зрения свои golden-тесты в ``test_utility_recognition``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from cp_helpers import make_counterparty, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    CashflowTransaction,
    CounterpartyPaymentDraft,
    DdsArticle,
    EmailInvoiceIntake,
    InvoicePaymentAllocation,
    Location,
    Organization,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    UtilityAccount,
)
from app.services import (
    bank_payment_status,
    email_invoice_ingest,
    supplier_prepayments,
    utility_intake,
    utility_ocr,
)
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.pnl.sources.recognition import ORIGIN_UTILITY, build_recognition_layer

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "utility"

# Сигнатура JPEG: приёмка определяет тип по содержимому, а не по имени файла и не по
# Content-Type — иначе SVG со скриптом, названный картинкой, вернулся бы inline в origin
# приложения. Хвост делает байты уникальными там, где тесту нужен «другой файл».
JPEG_HEAD = b"\xff\xd8\xff"
VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def _photo(tag: str) -> bytes:
    return JPEG_HEAD + f"фотография-{tag}".encode()


def _fixture(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


@pytest.fixture
def ocr_text(monkeypatch: pytest.MonkeyPatch):
    """Подменить распознавание заданным текстом. Возвращает функцию-установщик."""

    def setup(text: str | None, method: str = "vision") -> None:
        async def fake_extract(content, *, mime, settings):  # noqa: ANN001, ARG001
            return text, method

        monkeypatch.setattr(utility_ocr, "extract_text", fake_extract)

    return setup


async def _article(session: AsyncSession, *, name: str = "Коммунальные платежи") -> DdsArticle:
    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"art_{uuid.uuid4().hex[:8]}",
        name=name,
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
    )
    session.add(article)
    await session.flush()
    return article


async def _location(session: AsyncSession) -> Location:
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    location = Location(
        id=uuid.uuid4(), organization_id=organization_id, name=f"Черникова {uuid.uuid4().hex[:4]}"
    )
    session.add(location)
    await session.flush()
    return location


async def _account(
    session: AsyncSession,
    *,
    kind: str = "electricity",
    started_on: date = date(2026, 1, 1),
    is_active: bool = True,
    relationship: str = "informal",
    requisites: dict[str, str] | None = None,
    requisites_verified: bool = False,
) -> UtilityAccount:
    landlord = await make_counterparty(
        session,
        name=f"Гордеев {uuid.uuid4().hex[:4]}",
        inn=f"6143{uuid.uuid4().int % 10**8:08d}",
        cp_type="individual",
        relationship=relationship,
        requisites=requisites,
        requisites_verified=requisites_verified,
    )
    account = UtilityAccount(
        location_id=(await _location(session)).id,
        counterparty_id=landlord.id,
        kind=kind,
        dds_article_id=(await _article(session)).id,
        started_on=started_on,
        is_active=is_active,
    )
    session.add(account)
    await session.flush()
    return account


async def _ingest(
    session: AsyncSession, content: bytes, *, account_id: uuid.UUID | None = None
) -> list[EmailInvoiceIntake]:
    return await utility_intake.ingest_document(
        session,
        content=content,
        filename="IMG_0001.jpg",
        settings=get_settings(),
        account_id=account_id,
    )


async def _accruals(session: AsyncSession, *invoice_ids: uuid.UUID) -> list[SupplierExpenseAccrual]:
    return list(
        (
            await session.scalars(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id.in_(invoice_ids)
                )
            )
        ).all()
    )


async def _pay_bill(
    session: AsyncSession, bill: SupplierInvoice, *, on: date
) -> CashflowTransaction:
    """Оплатить счёт штатным чокпоинтом, чтобы его деньги стали дебиторкой."""
    wallet = await make_wallet(session, name="Тестовый банк")
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=bill.amount,
        operation_date=on,
        counterparty_id=bill.counterparty_id,
        source_kind="bank_feed",
    )
    session.add(transaction)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=bill.id,
            source_kind="bank",
            cashflow_transaction_id=transaction.id,
            amount=bill.amount,
        )
    )
    await session.flush()
    await supplier_prepayments.reconcile_bill_prepayment(session, bill)
    await session.flush()
    return transaction


async def test_actual_act_lands_in_payment_queue_with_both_amounts(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Акт за факт 17.07: в очередь оплат 30 402 ₽, в расход июня — 95 402 ₽.

    Это главное число всей ветки. Признай систему расход по сумме платёжки — июнь недосчитается
    65 000 ₽ зачтённого аванса, и недостача будет выглядеть экономией, а не ошибкой.
    """
    ocr_text(_fixture("electricity_real_20260717_actual.txt"))
    async with async_session_factory() as session:
        account = await _account(session)

        (intake,) = await _ingest(session, _photo("факт-июль"))

        assert intake.status == "linked", "счёт обязан попасть в очередь оплат"
        assert intake.mailbox == "photo"
        assert intake.utility_account_id == account.id
        # Контрагент — из потока (арендодатель), а НЕ энергосбыт из бумаги: платим не ему.
        assert intake.counterparty_id == account.counterparty_id

        bill = await session.get(SupplierInvoice, intake.invoice_id)
        closing = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert bill is not None and closing is not None
        assert (bill.doc_kind, closing.doc_kind) == ("bill", "closing")
        assert bill.amount == Decimal("30402.00")
        assert closing.amount == Decimal("95402.00")
        assert bill.counterparty_id == account.counterparty_id
        # Период — июнь, хотя акт подписан в июле: расход принадлежит месяцу потребления.
        assert (closing.service_period_start, closing.service_period_end) == (
            date(2026, 6, 1),
            date(2026, 6, 30),
        )

        accruals = await _accruals(session, bill.id, closing.id)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        assert accruals[0].amount == Decimal("95402.00")
        assert accruals[0].location_id == account.location_id
        await session.rollback()


async def test_advance_act_is_payable_but_carries_no_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Авансовый акт 17.07 — 65 000 ₽ к оплате и ни рубля расхода.

    Расход по июлю признает будущий акт за факт. Заведись он здесь — июль удвоился бы, а
    уплаченный вперёд аванс перестал бы быть дебиторкой, которую тот акт обязан зачесть.
    """
    ocr_text(_fixture("electricity_real_20260717_advance.txt"))
    async with async_session_factory() as session:
        await _account(session)

        (intake,) = await _ingest(session, _photo("аванс-июль"))

        assert intake.status == "linked"
        assert intake.companion_invoice_id is None, "у аванса закрывающего документа нет"
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        assert bill is not None
        assert bill.amount == Decimal("65000.00")
        assert bill.doc_kind == "bill"
        assert (bill.service_period_start, bill.service_period_end) == (
            date(2026, 7, 1),
            date(2026, 7, 31),
        )
        assert not await _accruals(session, bill.id)
        await session.rollback()


async def test_pair_of_photos_makes_two_payable_rows(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Два снимка одного визита — две строки очереди оплат: 30 402 и 65 000.

    Владелец платит их одним переводом, но документы разные: у них не совпадает ни период, ни
    роль. Слить в одну строку — показать сумму, которой нет ни в одной бумаге.
    """
    async with async_session_factory() as session:
        await _account(session)

        ocr_text(_fixture("electricity_real_20260717_actual.txt"))
        (actual,) = await _ingest(session, _photo("факт"))
        ocr_text(_fixture("electricity_real_20260717_advance.txt"))
        (advance,) = await _ingest(session, _photo("аванс"))

        assert {actual.status, advance.status} == {"linked"}
        bills = [
            await session.get(SupplierInvoice, actual.invoice_id),
            await session.get(SupplierInvoice, advance.invoice_id),
        ]
        assert sorted(bill.amount for bill in bills if bill) == [
            Decimal("30402.00"),
            Decimal("65000.00"),
        ]
        assert actual.recognition["utility"]["paired_intake_id"] == str(advance.id)
        assert advance.recognition["utility"]["paired_intake_id"] == str(actual.id)
        assert actual.recognition["utility"]["pair_payable_amount"] == "95402.00"
        assert any("Пара собрана" in hint for hint in advance.recognition["utility"]["hints"])
        await session.rollback()


async def test_pair_goes_to_bank_as_one_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Автопара проходит одним платежом через ДЗ/КЗ, ОПиУ и Баланс без смешения периодов.

    Предыдущий аванс июня закрывает часть июньского факта. Текущая пара — доплата июня и новый
    аванс июля — уходит одним переводом: доплата закрывает КЗ, новый аванс остаётся ДЗ, а в
    ОПиУ июня сохраняется полный расход 95 402 ₽ и ни рубля июльского аванса.
    """
    async with async_session_factory() as session:
        account = await _account(
            session,
            relationship="official",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=True,
        )

        # Предыдущий визит: аванс за июнь уже уплачен и живёт дебиторкой до фактического акта.
        ocr_text(_fixture("electricity_real_20260619_advance.txt"))
        (previous_advance,) = await _ingest(session, _photo("аванс-июнь"))
        previous_advance_bill = await session.get(SupplierInvoice, previous_advance.invoice_id)
        assert previous_advance_bill is not None
        await _pay_bill(session, previous_advance_bill, on=date(2026, 6, 20))

        # Текущий визит: факт июня + аванс июля автоматически сшиваются по дате и периодам.
        ocr_text(_fixture("electricity_real_20260717_actual.txt"))
        (actual,) = await _ingest(session, _photo("факт-вместе"))
        ocr_text(_fixture("electricity_real_20260717_advance.txt"))
        (advance,) = await _ingest(session, _photo("аванс-вместе"))

        before_payment = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        before_row = next(
            row
            for row in before_payment.rows
            if row.counterparty_id == account.counterparty_id
        )
        assert before_row.receivable == Decimal("0.00")
        assert before_row.payable == Decimal("30402.00")

        await session.commit()
        # Нажимаем «В банк» только на ОДНОЙ строке: сохранённая связь сама добавляет парную.
        await email_invoice_ingest.send_intake_to_bank(session, advance, actor_user_id=None)

        bills = [
            await session.get(SupplierInvoice, actual.invoice_id),
            await session.get(SupplierInvoice, advance.invoice_id),
        ]
        drafts = {bill.draft_id for bill in bills if bill}
        assert len(drafts) == 1 and None not in drafts, "перевод обязан быть один"
        draft = await session.get(CounterpartyPaymentDraft, drafts.pop())
        assert draft is not None
        assert draft.amount == Decimal("95402.00")
        # Периоды остались на счетах: по ним считается признание расхода.
        assert bills[0] is not None and bills[1] is not None
        assert bills[0].service_period_start == date(2026, 6, 1)
        assert bills[1].service_period_start == date(2026, 7, 1)

        bank_wallet = await make_wallet(session, name="Расчётный счёт", wallet_type="bank")

        async def resolved_wallet(session_arg, draft_arg):  # noqa: ANN001, ARG001
            return bank_wallet

        monkeypatch.setattr(bank_payment_status, "_resolve_payer_bank_wallet", resolved_wallet)
        assert (
            await bank_payment_status.apply_payment_status(
                session,
                draft=draft,
                raw_status="executed",
                operation_date=date(2026, 7, 17),
            )
            == "paid"
        )

        open_prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == account.counterparty_id,
                    SupplierPrepayment.status == "open",
                )
            )
        ).all()
        assert [(row.bill_invoice_id, row.amount) for row in open_prepayments] == [
            (bills[1].id, Decimal("65000.00"))
        ], "после оплаты открытой ДЗ остаётся только новый аванс июля"

        after_payment = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        after_row = next(
            row
            for row in after_payment.rows
            if row.counterparty_id == account.counterparty_id
        )
        assert after_row.payable == Decimal("0.00")
        assert after_row.receivable == Decimal("65000.00")

        june_pnl = await build_recognition_layer(
            session, date(2026, 6, 1), date(2026, 6, 30)
        )
        assert june_pnl.total == Decimal("95402.00")
        assert [detail.origin for detail in june_pnl.details] == [ORIGIN_UTILITY]
        await session.rollback()


async def test_same_periods_with_different_document_dates_are_not_paired(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Соседние периоды без общей даты актов — не доказанная пара, поэтому не угадываем."""
    async with async_session_factory() as session:
        await _account(session)
        ocr_text(_fixture("electricity_real_20260717_actual.txt"))
        (actual,) = await _ingest(session, _photo("факт-разные-даты"))
        advance_text = _fixture("electricity_real_20260717_advance.txt").replace(
            "17.07.2026", "18.07.2026"
        )
        ocr_text(advance_text)
        (advance,) = await _ingest(session, _photo("аванс-разные-даты"))

        assert not actual.recognition["utility"].get("paired_intake_id")
        assert not advance.recognition["utility"].get("paired_intake_id")
        await session.rollback()


async def test_same_file_twice_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Тот же файл второй раз — отказ, а не вторая строка."""
    ocr_text(_fixture("electricity_real_20260717_advance.txt"))
    async with async_session_factory() as session:
        await _account(session)
        photo = _photo("аванс-июль")
        await _ingest(session, photo)

        with pytest.raises(utility_intake.UtilityIntakeError, match="уже загружали"):
            await _ingest(session, photo)
        await session.rollback()


async def test_rephotographed_receipt_becomes_duplicate(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Пересняли ту же бумагу — строка есть, второго долга нет.

    Дедуп по содержимому файла тут бессилен: байты другие. Ловит месяц — ключ идемпотентности
    документа. Строка при этом обязана уйти из очереди оплат (статус ``duplicate``), иначе один
    месяц предъявится к оплате дважды.
    """
    ocr_text(_fixture("electricity_real_20260717_actual.txt"))
    async with async_session_factory() as session:
        await _account(session)

        (first,) = await _ingest(session, _photo("снимок-1"))
        (second,) = await _ingest(session, _photo("снимок-2-та-же-бумага"))

        assert first.status == "linked"
        assert second.status == "duplicate"
        assert second.invoice_id == first.invoice_id, "дубль ссылается на уже заведённый счёт"
        assert second.companion_invoice_id is None
        bills = (
            await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.source == "utility",
                    SupplierInvoice.doc_kind == "bill",
                )
            )
        ).all()
        assert len(bills) == 1, "второй счёт за тот же месяц заводить нельзя"
        await session.rollback()


async def test_water_expense_equals_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """У воды расход и платёж — одно число: счёт месяца и есть расход месяца."""
    ocr_text(_fixture("water_real_20260523_192636_b198d13d.txt"))
    async with async_session_factory() as session:
        await _account(session, kind="water")

        (intake,) = await _ingest(session, _photo("вода"))

        assert intake.status == "linked"
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        closing = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert bill is not None and closing is not None
        assert bill.amount == closing.amount
        accruals = await _accruals(session, bill.id, closing.id)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        await session.rollback()


async def test_no_flow_for_resource_waits_for_hands(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Поток не заведён — документов нет, но строка есть и объясняет причину.

    Угадывать тут нечего: поток несёт помещение, получателя денег и статью. Ошибка отправила бы
    деньги другому арендодателю и посадила расход на чужую точку.
    """
    ocr_text(_fixture("electricity_real_20260717_actual.txt"))
    async with async_session_factory() as session:
        await _account(session, kind="water")  # есть только вода

        (intake,) = await _ingest(session, _photo("свет-без-потока"))

        assert intake.status == "needs_review"
        assert intake.invoice_id is None
        assert intake.utility_account_id is None
        assert "Не заведён поток для этого ресурса" in intake.recognition["utility"]["blocking"]
        await session.rollback()


async def test_two_flows_of_one_resource_are_never_guessed(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Две точки с электричеством — выбор за человеком, а не за системой.

    А когда поток назвали явно, он и берётся: владелец знает, чью квитанцию принёс.
    """
    ocr_text(_fixture("electricity_real_20260717_actual.txt"))
    async with async_session_factory() as session:
        first = await _account(session)
        await _account(session)

        (ambiguous,) = await _ingest(session, _photo("две-точки"))
        assert ambiguous.status == "needs_review"
        assert (
            "Потоков этого ресурса несколько — выберите нужный"
            in (ambiguous.recognition["utility"]["blocking"])
        )

        (chosen,) = await _ingest(session, _photo("две-точки-выбрана"), account_id=first.id)
        assert chosen.status == "linked"
        assert chosen.utility_account_id == first.id
        await session.rollback()


async def test_unreadable_photo_still_leaves_a_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Не прочиталось — строка со снимком всё равно есть.

    Молча потерянный документ хуже неразобранного: о неразобранном хотя бы известно.
    """
    ocr_text(None, method="vision_failed")
    async with async_session_factory() as session:
        await _account(session)

        (intake,) = await _ingest(session, _photo("смазанный-снимок"))

        assert intake.status == "needs_review"
        assert intake.pdf_bytes is not None, "снимок обязан сохраниться — по нему вводят руками"
        assert "Не удалось прочитать текст со снимка" in intake.recognition["utility"]["blocking"]
        await session.rollback()


async def test_foreign_paper_is_not_dragged_into_utilities(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Снимок обычного счёта поставщика коммунальным не становится.

    Ресурс не опознан — значит и потока у строки нет, и провести её коммунальной дверью нельзя:
    она пойдёт обычным путём «Страницы на оплату», как счёт из письма.
    """
    ocr_text(_fixture("synthetic_invoice.txt"))
    async with async_session_factory() as session:
        await _account(session)

        (intake,) = await _ingest(session, _photo("накладная-от-поставщика"))

        assert intake.status == "needs_review"
        assert intake.utility_account_id is None
        assert not intake.recognition["utility"].get("kind")
        await session.rollback()


async def test_manual_confirm_provides_what_recognition_could_not(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Газ приходит без бумаги — суммы называет человек, и документы заводятся те же.

    Ручной путь обязан быть равноправным, а не аварийным: у газа документа нет вовсе, и других
    способов завести долг по нему не существует.
    """
    ocr_text(None, method="vision_failed")
    async with async_session_factory() as session:
        account = await _account(session, kind="gas")
        (intake,) = await _ingest(session, _photo("газ-со-слов"))

        status = await utility_intake.confirm_utility_intake(
            session,
            intake,
            account_id=account.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            expense_amount=Decimal("4200.00"),
            payable_amount=Decimal("4200.00"),
        )

        assert status == "linked"
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        closing = await session.get(SupplierInvoice, intake.companion_invoice_id)
        assert bill is not None and closing is not None
        assert bill.amount == closing.amount == Decimal("4200.00")
        assert bill.number == "Возмещение: газ, 06.2026"
        assert intake.counterparty_id == account.counterparty_id
        accruals = await _accruals(session, bill.id, closing.id)
        assert [accrual.invoice_id for accrual in accruals] == [closing.id]
        await session.rollback()


async def test_manual_confirm_of_advance_creates_no_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Пустой расход при ручном проведении — это аванс: платим, но расхода не признаём."""
    ocr_text(None, method="vision_failed")
    async with async_session_factory() as session:
        account = await _account(session)
        (intake,) = await _ingest(session, _photo("аванс-руками"))

        await utility_intake.confirm_utility_intake(
            session,
            intake,
            account_id=account.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            expense_amount=None,
            payable_amount=Decimal("65000.00"),
        )

        assert intake.companion_invoice_id is None
        bill = await session.get(SupplierInvoice, intake.invoice_id)
        assert bill is not None
        assert not await _accruals(session, bill.id)
        await session.rollback()


async def test_manual_confirm_twice_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Проведённую строку вторым нажатием не проводят: месяц уже занят, долг уже есть."""
    ocr_text(None, method="vision_failed")
    async with async_session_factory() as session:
        account = await _account(session, kind="gas")
        (intake,) = await _ingest(session, _photo("газ-дважды"))
        await utility_intake.confirm_utility_intake(
            session,
            intake,
            account_id=account.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            expense_amount=Decimal("4200.00"),
            payable_amount=Decimal("4200.00"),
        )

        with pytest.raises(utility_intake.UtilityIntakeError, match="уже проведена"):
            await utility_intake.confirm_utility_intake(
                session,
                intake,
                account_id=account.id,
                period_start=date(2026, 6, 1),
                period_end=date(2026, 6, 30),
                expense_amount=Decimal("4200.00"),
                payable_amount=Decimal("4200.00"),
            )
        await session.rollback()


async def test_recognised_amounts_are_not_taken_from_the_receipt_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
    ocr_text,
) -> None:
    """Реквизиты ресурсника остаются справкой и в платёж не идут.

    В акте напечатан ИНН энергосбыта — платёж по нему ушёл бы постороннему юрлицу. Банковский
    блок обязан остаться пустым: реквизиты берутся из карточки арендодателя.
    """
    ocr_text(_fixture("electricity_real_20260717_actual.txt"))
    async with async_session_factory() as session:
        account = await _account(session)

        (intake,) = await _ingest(session, _photo("реквизиты"))

        recognition = intake.recognition
        assert recognition["requisites"] == {}
        assert recognition["utility"]["source_org"]["inn"] == "614314309921"
        landlord_inn = await session.scalar(
            select(SupplierInvoice.counterparty_id).where(SupplierInvoice.id == intake.invoice_id)
        )
        assert landlord_inn == account.counterparty_id
        await session.rollback()
