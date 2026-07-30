"""Реквизиты из истории: раскладка ключей по банкам и поиск кандидатов.

Главное, что здесь закреплено, — БИК берётся ТОЛЬКО из блока контрагента. У Т-Банка на
верхнем уровне payload лежит ключ ``bic`` со значением БИК самого Т-Банка (одинаковый
у всех операций), и прежняя эвристика «поискать bic где-нибудь» подставляла в карточку
его: банк получателя выходил чужой, а корр-счёт пустой (ключа ``corrAccount`` у Т-Банка
нет — там ``corAcct``). Тестов на то автозаполнение не было ни одного, поэтому баг
жил в проде незамеченным.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from cp_helpers import admin_headers, make_bank_operation, make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake
from app.services.counterparty_requisites_history import (
    requisites_from_operation,
    search_history_requisites,
)

# Реальная раскладка Т-Банка (подтверждена выпиской прода: 1144 из 1147 исходящих).
TBANK_RECEIVER = {
    "name": 'ООО "МЕТРО КЭШ ЭНД КЕРРИ"',
    "inn": "7704218694",
    "kpp": "770401001",
    "acct": "40702810100000012345",
    "bicRu": "044525225",
    "corAcct": "30101810400000000225",
    "bankName": 'ПАО "СБЕРБАНК"',
}
# БИК самого Т-Банка: он есть в КАЖДОЙ операции на верхнем уровне и к получателю
# отношения не имеет.
TBANK_OWN_BIC = {"bic": "044525974"}

SBER_TRANSFER = {
    "payeeName": "ИП Егиазарян Гарик Ваграмович",
    "payeeInn": "614307902094",
    "payeeKpp": "0",
    "payeeAccount": "40802810100002438573",
    "payeeBankBic": "046015602",
    "payeeBankCorrAccount": "30101810600000000602",
    "payeeBankName": "ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК",
}


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _make_intake(
    session: AsyncSession, *, requisites: dict[str, str], amount: str = "12000.00"
) -> EmailInvoiceIntake:
    intake = EmailInvoiceIntake(
        mailbox="corporate",
        attachment_sha256=f"sha-{requisites.get('inn')}-{requisites.get('bankAcnt')}",
        status="recognized",
        received_at=datetime(2026, 7, 10, tzinfo=UTC),
        recognition={"amount": amount, "requisites": requisites},
    )
    session.add(intake)
    await session.flush()
    return intake


async def test_tbank_payee_requisites_ignore_own_bank_bic(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        operation = await make_bank_operation(
            session,
            amount="5000.00",
            inn="7704218694",
            name='ООО "МЕТРО КЭШ ЭНД КЕРРИ"',
            receiver=TBANK_RECEIVER,
            raw_payload=dict(TBANK_OWN_BIC),
        )

        requisites = requisites_from_operation(operation)

        assert requisites["bankBik"] == "044525225"  # банк ПОЛУЧАТЕЛЯ, не наш
        assert requisites["recipientCorrAccountNumber"] == "30101810400000000225"
        assert requisites["bankAcnt"] == "40702810100000012345"
        assert requisites["kpp"] == "770401001"
        assert requisites["recipientName"] == 'ООО "МЕТРО КЭШ ЭНД КЕРРИ"'


async def test_sber_requisites_come_from_rur_transfer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        operation = await make_bank_operation(
            session,
            amount="8000.00",
            provider="sber",
            inn="614307902094",
            raw_payload={"rurTransfer": SBER_TRANSFER},
        )

        requisites = requisites_from_operation(operation)

        assert requisites["bankBik"] == "046015602"
        assert requisites["recipientCorrAccountNumber"] == "30101810600000000602"
        assert requisites["bankAcnt"] == "40802810100002438573"
        # Сбер пишет отсутствующий КПП как «0» — это заглушка схемы, а не реквизит.
        assert "kpp" not in requisites


async def test_incoming_operation_describes_payer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У поступления контрагент — плательщик: для дебитора реквизиты берутся из его блока."""
    async with async_session_factory() as session:
        operation = await make_bank_operation(
            session,
            amount="3000.00",
            direction="in",
            raw_payload={
                "payer": {
                    "name": "ООО Покупатель",
                    "inn": "7712345678",
                    "acct": "40702810900000055555",
                    "bicRu": "044525593",
                },
                "receiver": {"name": "МЫ", "inn": "890307589201"},
            },
        )

        requisites = requisites_from_operation(operation)

        assert requisites["recipientName"] == "ООО Покупатель"
        assert requisites["inn"] == "7712345678"
        assert requisites["bankBik"] == "044525593"


async def test_search_by_inn_returns_complete_candidate(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await make_bank_operation(
            session,
            amount="5000.00",
            inn="7704218694",
            receiver=TBANK_RECEIVER,
            raw_payload=dict(TBANK_OWN_BIC),
            operation_date=date(2026, 7, 20),
        )
        await session.commit()

        candidates = await search_history_requisites(session, "7704218694")

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.missing == []
        assert candidate.source == "bank"
        assert candidate.bank_name == 'ПАО "СБЕРБАНК"'
        assert candidate.last_seen_on == date(2026, 7, 20)
        assert candidate.existing_counterparty_id is None


async def test_search_by_name_flags_existing_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дедуп до 409: ИНН уникален в реестре, о существующей карточке надо сказать заранее."""
    async with async_session_factory() as session:
        existing = await make_counterparty(session, name="МЕТРО", inn="7704218694")
        await make_bank_operation(
            session,
            amount="5000.00",
            inn="7704218694",
            # Поиск по названию идёт по нормализованной колонке: банк-специфичный JSON
            # для этого не нужен — оба провайдера заполняют её при импорте выписки.
            name='ООО "МЕТРО КЭШ ЭНД КЕРРИ"',
            receiver=TBANK_RECEIVER,
        )
        await session.commit()

        candidates = await search_history_requisites(session, "МЕТРО КЭШ")

        assert len(candidates) == 1
        assert candidates[0].existing_counterparty_id == existing.id
        assert candidates[0].existing_counterparty_name == "МЕТРО"


async def test_search_skips_card_noise_unless_asked_by_inn(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await make_bank_operation(
            session,
            amount="1000.00",
            inn="7710140679",
            name='АО "ТБанк"',
            receiver={"inn": "7710140679", "name": 'АО "ТБанк"', "acct": "40702810100000000001"},
        )
        await make_bank_operation(
            session,
            amount="900.00",
            inn="7704218694",
            name="Кафе на углу",
            receiver=TBANK_RECEIVER,
            category="cardOperation",
        )
        await session.commit()

        assert await search_history_requisites(session, "ТБанк") == []
        assert await search_history_requisites(session, "Кафе на углу") == []
        # Прямой поиск по ИНН банка — человек знает, чего просит.
        by_inn = await search_history_requisites(session, "7710140679")
        assert len(by_inn) == 1


async def test_search_splits_accounts_and_backfills_kpp(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Смена счёта поставщиком — отдельный кандидат (молча подставить старый нельзя),
    а КПП добирается из прошлых платёжек: банк присылает его не в каждой."""
    async with async_session_factory() as session:
        await make_bank_operation(
            session,
            amount="1000.00",
            inn="7704218694",
            receiver={**TBANK_RECEIVER, "kpp": "770401001"},
            operation_date=date(2026, 5, 1),
        )
        await make_bank_operation(
            session,
            amount="2000.00",
            inn="7704218694",
            receiver={key: value for key, value in TBANK_RECEIVER.items() if key != "kpp"},
            operation_date=date(2026, 6, 1),
        )
        await make_bank_operation(
            session,
            amount="3000.00",
            inn="7704218694",
            receiver={**TBANK_RECEIVER, "acct": "40702810100000099999"},
            operation_date=date(2026, 7, 1),
        )
        await session.commit()

        candidates = await search_history_requisites(session, "7704218694")

        assert len(candidates) == 2
        newest = candidates[0]
        assert newest.last_seen_on == date(2026, 7, 1)
        assert newest.requisites["bankAcnt"] == "40702810100000099999"
        older = candidates[1]
        assert older.hits == 2
        assert older.requisites["kpp"] == "770401001"


async def test_search_escapes_like_wildcards(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«%» в запросе — символ названия, а не маска «найди всё»."""
    async with async_session_factory() as session:
        await make_bank_operation(
            session, amount="1000.00", inn="7704218694", receiver=TBANK_RECEIVER
        )
        await session.commit()

        assert await search_history_requisites(session, "%%%") == []


async def test_search_finds_email_invoice_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Поставщику ещё не платили — реквизиты берутся из распознанного счёта из почты."""
    async with async_session_factory() as session:
        await _make_intake(
            session,
            requisites={
                "recipientName": 'ООО "СИТИ"',
                "inn": "6143059250",
                "bankAcnt": "40702810300000077777",
                "bankBik": "046015602",
                "recipientCorrAccountNumber": "30101810600000000602",
            },
        )
        await session.commit()

        candidates = await search_history_requisites(session, "6143059250")

        assert len(candidates) == 1
        assert candidates[0].source == "email"
        assert candidates[0].source_label == "Счёт из почты"
        assert candidates[0].missing == []


def test_search_endpoint_is_not_shadowed_by_card_route(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """``/requisites/search`` объявлен до ``/{counterparty_id}`` — иначе 422 на разборе UUID."""

    async def seed() -> None:
        async with async_session_factory() as session:
            await make_bank_operation(
                session,
                amount="5000.00",
                inn="7704218694",
                receiver=TBANK_RECEIVER,
                raw_payload=dict(TBANK_OWN_BIC),
            )
            await session.commit()

    asyncio.run(seed())
    headers = _admin(async_session_factory)

    response = client.get(
        "/api/v1/counterparties/requisites/search",
        params={"query": "7704218694"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["requisites"]["bankBik"] == "044525225"
    assert body[0]["missing"] == []


def test_search_endpoint_requires_admin(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = client.get(
        "/api/v1/counterparties/requisites/search", params={"query": "7704218694"}
    )
    assert response.status_code in (401, 403)
