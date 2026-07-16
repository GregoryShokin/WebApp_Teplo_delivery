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

from app.models import SbisDocument
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
