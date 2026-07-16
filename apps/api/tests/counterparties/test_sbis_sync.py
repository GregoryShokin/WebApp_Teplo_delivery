"""Зеркало СБИС ЭДО: парсинг реестра, идемпотентный апсерт и сверка с накладными.

Структура тестового реестрового элемента — точная копия живого ответа
«СБИС.СписокДокументовПоСобытиям» (спайк 2026-07-16, ИП-контрагент в СвФЛ).
Матчинг: number_amount (номер+сумма), amount_date (сумма+окно дат, только
единственный кандидат), коллизия сумм НЕ матчится.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SbisDocument, SupplierInvoice
from app.services.sbis.sync import (
    SbisSyncResult,
    _match_documents,
    _upsert_documents,
    normalize_number,
)


def _registry_item(
    *,
    doc_id: str = "019f69a7-5e3d-74f1-bcca-a1612a488da0",
    number: str = "ЦБ-9437",
    amount: str = "121313.24",
    doc_date: str = "15.07.2026",
    doc_type: str = "ДокОтгрВх",
    deleted: str = "Нет",
) -> dict[str, Any]:
    return {
        "ДатаВремя": "16.07.2026 09.40.10",
        "Состояние": {"Код": "10", "Название": "В обработке"},
        "Документ": {
            "Идентификатор": doc_id,
            "Дата": doc_date,
            "Номер": number,
            "Сумма": amount,
            "Тип": doc_type,
            "Удален": deleted,
            "Название": f"Поступление № {number} от {doc_date}",
            "Направление": "Входящий",
            "Регламент": {"Название": "Поступление"},
            "Состояние": {"Код": "10", "Название": "В обработке"},
            "СсылкаДляНашаОрганизация": "https://online.sbis.ru/opendoc.html?guid=x",
            "Контрагент": {
                "Тип": "ИП",
                "СвФЛ": {
                    "ИНН": "231006560100",
                    "НазваниеПолное": "Буряк Эдуард Николаевич",
                    "Фамилия": "Буряк",
                    "Имя": "Эдуард",
                    "Отчество": "Николаевич",
                },
            },
            "Вложение": [
                {
                    "Служебный": "Нет",
                    "Тип": "УпдСчфДоп",
                    "Номер": number,
                    "Сумма": amount,
                    "СуммаБезНДС": "103370.15",
                    "СсылкаНаPDF": "https://online.sbis.ru/pdfservicepublic/service/?x",
                    "Файл": {"Ссылка": "https://disk.sbis.ru/disk/api/v1/x"},
                }
            ],
        },
    }


def test_normalize_number() -> None:
    assert normalize_number(" ЦБ-9437 ") == "цб-9437"
    assert normalize_number("МСК 0715/0534") == "мск0715/0534"
    assert normalize_number(None) is None
    assert normalize_number("  ") is None


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(SbisDocument))


async def test_upsert_idempotent_and_parses_fields(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.commit()
        assert result.created == 1

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert doc.sbis_doc_id == "019f69a7-5e3d-74f1-bcca-a1612a488da0"
        assert doc.doc_type == "ДокОтгрВх"
        assert doc.regulation == "Поступление"
        assert doc.number == "ЦБ-9437"
        assert doc.doc_date == date(2026, 7, 15)
        assert doc.amount == Decimal("121313.24")
        assert doc.amount_wo_vat == Decimal("103370.15")
        assert doc.counterparty_name == "Буряк Эдуард Николаевич"
        assert doc.counterparty_inn == "231006560100"
        assert doc.state_name == "В обработке"
        assert doc.attachment_kind == "УпдСчфДоп"
        assert doc.link_pdf and doc.link_xml and doc.link_cabinet
        assert doc.match_status == "unmatched"

        # Повторный проход того же окна — обновление, не дубль.
        result2 = SbisSyncResult()
        await _upsert_documents(session, [_registry_item(amount="121999.00")], result2)
        await session.commit()
        assert result2.created == 0 and result2.updated == 1
        assert await _count(session) == 1
        await session.refresh(doc)
        assert doc.amount == Decimal("121999.00")


async def test_upsert_skips_deleted(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = SbisSyncResult()
        await _upsert_documents(
            session, [_registry_item(doc_id="dead-doc", deleted="Да")], result
        )
        await session.commit()
        assert result.skipped_deleted == 1
        assert await _count(session) == 0


async def test_match_by_number_and_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Буряк", inn="231006560100")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="121313.24",
            number="ЦБ-9437",
            source="iiko",
            invoice_date=date(2026, 7, 15),
        )
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        await _match_documents(session, result)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert result.matched == 1
        assert doc.match_status == "matched"
        assert doc.matched_invoice_id == invoice.id
        assert doc.match_note == "number_amount"


async def test_match_by_amount_within_date_window(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Буряк", inn="231006560100")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="121313.24",
            number="СОВСЕМ-ДРУГОЙ",  # iiko-номер не совпал — сработает окно дат
            source="iiko",
            invoice_date=date(2026, 7, 17),
        )
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        await _match_documents(session, result)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert doc.match_status == "matched"
        assert doc.matched_invoice_id == invoice.id
        assert doc.match_note == "amount_date"


async def test_amount_collision_stays_unmatched(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две накладные с одинаковой суммой в окне дат — не угадываем, оставляем оператору."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Буряк", inn="231006560100")
        for number in ("А-1", "А-2"):
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="121313.24",
                number=number,
                source="iiko",
                invoice_date=date(2026, 7, 15),
            )
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        await _match_documents(session, result)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert result.matched == 0
        assert doc.match_status == "unmatched"
        assert doc.matched_invoice_id is None


async def test_matched_docs_not_rematched(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручную/старую связку повторный синк не перетирает."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Буряк", inn="231006560100")
        invoice_a = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="121313.24",
            number="ЦБ-9437",
            source="iiko",
            invoice_date=date(2026, 7, 15),
        )
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        await _match_documents(session, result)
        await session.commit()

        # Появилась вторая идентичная накладная — второй прогон не должен трогать связку.
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="121313.24",
            number="ЦБ-9437",
            source="iiko",
            invoice_date=date(2026, 7, 15),
        )
        result2 = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result2)
        await session.flush()
        await _match_documents(session, result2)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert result2.matched == 0
        assert doc.matched_invoice_id == invoice_a.id


# --- Маршрутизация: режим определяет карточка контрагента (канал 'sbis') -----------------


async def _route(session) -> SbisSyncResult:
    from app.services.sbis.sync import _route_documents

    result = SbisSyncResult()
    await _route_documents(session, result)
    return result


async def test_unknown_inn_creates_requires_setup_placeholder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import Counterparty

    async with async_session_factory() as session:
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.new_counterparties == 1
        assert doc.intake_status == "new_counterparty"
        placeholder = (
            await session.execute(
                select(Counterparty).where(Counterparty.inn == "231006560100")
            )
        ).scalar_one()
        assert placeholder.status == "requires_setup"
        assert placeholder.type == "individual"  # ИНН 12 знаков = ИП
        assert doc.counterparty_id == placeholder.id
        # Накопленный документ НЕ материализован — канал не включён.
        assert doc.invoice_id is None


async def test_channel_counterparty_materializes_invoice_with_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import CounterpartyCollectionSource, CounterpartyPayableProfile

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Манго Телеком", inn="231006560100")
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=cp.id, kind="sbis", value="231006560100"
            )
        )
        profile = (
            await session.execute(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == cp.id
                )
            )
        ).scalar_one()
        profile.service_period_required = True
        await session.flush()

        item = _registry_item()
        item["Документ"]["Название"] = "Услуги связи за июнь 2026"
        result = SbisSyncResult()
        await _upsert_documents(session, [item], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.materialized == 1
        assert doc.intake_status == "materialized"
        invoice = await session.get(SupplierInvoice, doc.invoice_id)
        assert invoice is not None
        assert invoice.source == "sbis"
        assert invoice.external_id == doc.sbis_doc_id
        assert invoice.amount == Decimal("121313.24")
        # Период распознан regex-слоем из названия («за июнь 2026»).
        assert invoice.service_period_status == "ready"
        assert invoice.service_period_start == date(2026, 6, 1)
        assert invoice.service_period_end == date(2026, 6, 30)
        # Начисление признания создано (ядро ветки периодов).
        from app.models import SupplierExpenseAccrual

        accrual = (
            await session.execute(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == invoice.id
                )
            )
        ).scalar_one_or_none()
        assert accrual is not None


async def test_channel_materialization_dedups_against_email_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import CounterpartyCollectionSource

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ЛЕММА", inn="231006560100")
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=cp.id, kind="sbis", value="231006560100"
            )
        )
        email_invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="121313.24",
            number="ЦБ-9437",
            source="email",
            invoice_date=date(2026, 7, 15),
        )
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.duplicates == 1
        assert route_result.materialized == 0
        assert doc.intake_status == "duplicate"
        assert doc.invoice_id == email_invoice.id
        count = await session.scalar(
            select(func.count()).select_from(SupplierInvoice)
        )
        assert count == 1  # второй счёт НЕ создан


async def test_counterparty_without_channel_stays_mirror(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Буряк", inn="231006560100")
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.materialized == 0
        assert doc.intake_status == "mirror"
        assert doc.counterparty_id == cp.id
        assert doc.invoice_id is None


async def test_new_counterparty_backfills_after_setup(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оператор настроил карточку и включил канал → накопленное материализуется."""
    from app.models import Counterparty, CounterpartyCollectionSource

    async with async_session_factory() as session:
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        await _route(session)
        await session.commit()

        placeholder = (
            await session.execute(
                select(Counterparty).where(Counterparty.inn == "231006560100")
            )
        ).scalar_one()
        placeholder.status = "active"
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=placeholder.id, kind="sbis", value="231006560100"
            )
        )
        await session.flush()

        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.materialized == 1
        assert doc.intake_status == "materialized"
        assert doc.invoice_id is not None


async def test_archived_counterparty_not_materialized_even_with_channel(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Архив = блок новых накладных: канал включён, но счёт не создаём."""
    from app.models import CounterpartyCollectionSource

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="ИП Билинский", inn="231006560100", status="archived"
        )
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=cp.id, kind="sbis", value="231006560100"
            )
        )
        await session.flush()
        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert route_result.materialized == 0
        assert doc.intake_status == "mirror"
        assert doc.invoice_id is None


async def test_materialized_invoice_auto_settles_from_open_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Закрывающий документ»: поставщик оплачен авансом → УПД гасит дебиторку,
    счёт не попадает «к оплате». Денег не двигает."""
    from app.models import CounterpartyCollectionSource, SupplierPrepayment

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Стартер", inn="231006560100")
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=cp.id, kind="sbis", value="231006560100"
            )
        )
        prepayment = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="goods",
            amount=Decimal("200000.00"),
            amount_settled=Decimal("0.00"),
            status="open",
        )
        session.add(prepayment)
        await session.flush()

        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        invoice = await session.get(SupplierInvoice, doc.invoice_id)
        assert route_result.materialized == 1
        assert route_result.settled_from_prepayments == 1
        assert invoice is not None
        assert invoice.payment_status == "paid"  # закрыт предоплатой целиком
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("121313.24")
        assert prepayment.status == "partially_settled"


async def test_materialized_invoice_partial_prepayment_coverage(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Предоплата меньше счёта: гасим сколько есть, остаток остаётся «к оплате»."""
    from app.models import CounterpartyCollectionSource, SupplierPrepayment

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Стартер", inn="231006560100")
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=cp.id, kind="sbis", value="231006560100"
            )
        )
        prepayment = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="goods",
            amount=Decimal("100000.00"),
            amount_settled=Decimal("0.00"),
            status="open",
        )
        session.add(prepayment)
        await session.flush()

        result = SbisSyncResult()
        await _upsert_documents(session, [_registry_item()], result)
        await session.flush()
        route_result = await _route(session)
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        invoice = await session.get(SupplierInvoice, doc.invoice_id)
        assert route_result.settled_from_prepayments == 1
        assert invoice is not None
        assert invoice.payment_status == "partially_paid"
        await session.refresh(prepayment)
        assert prepayment.status == "settled"
        assert prepayment.amount_settled == Decimal("100000.00")


async def test_invoice_letter_routed_to_recognition(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Письмо (КоррВх) с вложением-счётом у канального контрагента уходит в распознавание
    «Страницы на оплату» (сумма только в PDF); идемпотентно по SHA-256 файла."""
    from app.models import CounterpartyCollectionSource, EmailInvoiceIntake
    from app.services.sbis.sync import _route_documents

    class FakeClient:
        async def download_file(self, url: str) -> bytes:
            return b"%PDF-1.7 fake invoice"

    letter = {
        "ДатаВремя": "18.06.2026 10.00.00",
        "Состояние": {"Код": "5", "Название": "Доставлен"},
        "Документ": {
            "Идентификатор": "letter-15758",
            "Дата": "18.06.2026",
            "Номер": "15758",
            "Тип": "КоррВх",
            "Удален": "Нет",
            "Название": "Письмо № 15758 от 18.06.2026",
            "Контрагент": {
                "СвЮЛ": {"ИНН": "7802193688", "Название": "ООО ДОКСИНБОКС"}
            },
            "Вложение": [
                {
                    "Служебный": "Нет",
                    "Название": "Счет-оферта_№0006634309 _от 18.06.2026.pdf",
                    "Файл": {
                        "Имя": "Счет-оферта_№0006634309 _от 18.06.2026.pdf",
                        "Ссылка": "https://disk.sbis.ru/x",
                    },
                }
            ],
        },
    }

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ДоксИнБокс", inn="7802193688")
        session.add(
            CounterpartyCollectionSource(counterparty_id=cp.id, kind="sbis", value="7802193688")
        )
        await session.flush()
        result = SbisSyncResult()
        await _upsert_documents(session, [letter], result)
        await session.flush()
        await _route_documents(session, result, FakeClient())
        await session.commit()

        doc = (await session.execute(select(SbisDocument))).scalar_one()
        assert result.sent_to_recognition == 1
        assert doc.intake_status == "sent_to_recognition"
        intake = (await session.execute(select(EmailInvoiceIntake))).scalar_one()
        assert intake.mailbox == "sbis"
        assert intake.attachment_filename.startswith("Счет-оферта")

        # Повторный проход — новый intake не создаётся (SHA-дедуп).
        doc.intake_status = "mirror"
        await session.flush()
        result2 = SbisSyncResult()
        await _route_documents(session, result2, FakeClient())
        await session.commit()
        assert result2.sent_to_recognition == 0
        count = await session.scalar(select(func.count()).select_from(EmailInvoiceIntake))
        assert count == 1
        await session.refresh(doc)
        assert doc.intake_status == "sent_to_recognition"
