"""Переоценка ОС по свободному тексту менеджера: модель предлагает, человек решает.

Сеть здесь не трогается ни разу: вызов модели инъектируется параметром ``call`` — контур,
двигающий стоимость активов, обязан проверяться без похода наружу.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import admin_headers  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.models import AssetConditionReport, FixedAsset  # noqa: E402
from app.services.anthropic_client import LlmCallError  # noqa: E402
from app.services.asset_revaluation import (  # noqa: E402
    build_prompt,
    build_purchase_prompt,
    decide_report,
    process_pending,
    submit_report,
)

BASE = "/api/v1/fixed-assets"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _asset(session: AsyncSession, *, cost: str = "100000.00") -> FixedAsset:
    asset = FixedAsset(
        name="Шкаф холодильный",
        brand_model="POLAIR CM107-S",
        initial_cost=Decimal(cost),
        useful_life_months=84,
        commissioned_on=date(2026, 8, 1),
        status="in_use",
        valuation_basis="market",
    )
    session.add(asset)
    await session.flush()
    return asset


def _answer(**overrides: Any):
    payload = {
        "impact_kind": "breakdown",
        "value_loss_share": "0.8",
        "reasoning": "Отказал компрессор — без него шкаф не держит холод и стоит как железо.",
        "confidence": 0.85,
        "needs_human": False,
    }
    payload.update(overrides)

    async def call(_settings, **_kwargs) -> dict[str, Any]:
        return payload

    return call


async def test_prompt_carries_precomputed_money_and_forbids_arithmetic(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модель не считает деньги — все суммы уходят в промпт готовыми.

    Правило проекта, а не осторожность: «LLM ненадёжна в арифметике, её дело — интерпретация,
    не вычисления». У модели просим только долю потери стоимости.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        report = await submit_report(
            session, asset_id=asset.id, message="Сломался компрессор", user_id=None
        )
        await session.commit()

        prompt = await build_prompt(session, report)
        assert "ОСТАТОЧНАЯ СТОИМОСТЬ: 100000.00 ₽" in prompt
        assert "Начислено амортизации: 0.00 ₽" in prompt
        assert "POLAIR CM107-S" in prompt
        assert "Сломался компрессор" in prompt
        assert "Суммы НЕ вычисляй" in prompt


async def test_code_computes_the_sum_from_the_share(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модель дала долю 0.8 — сумму считает код: 100 000 × (1 − 0.8) = 20 000."""
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="Отказал компрессор", user_id=None)
        await session.commit()

        counts = await process_pending(session, call=_answer())
        assert counts == {"processed": 1, "proposed": 1, "failed": 0}

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.status == "proposed"
        assert Decimal(str(report.proposed_cost)) == Decimal("20000.00")
        assert Decimal(str(report.confidence)) == Decimal("0.850")
        # Стоимость объекта пока НЕ изменилась: решение за владельцем.
        assert Decimal(str(asset.initial_cost)) == Decimal("100000.00")


async def test_unclear_message_reaches_the_owner_without_a_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модель не смогла оценить — запись всё равно доходит до владельца.

    Сообщение менеджера о поломке ценно само по себе, даже когда в деньгах его оценить нельзя.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="что-то не то", user_id=None)
        await session.commit()

        await process_pending(session, call=_answer(needs_human=True))

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.status == "proposed"
        assert report.proposed_cost is None


async def test_absurd_share_is_thrown_away_not_applied(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доля вне 0..1 — ответ негодный, предложения по деньгам нет.

    Модель не обязана соблюдать схему, а её ошибка не должна стать стоимостью актива.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="сломалось", user_id=None)
        await session.commit()

        await process_pending(session, call=_answer(value_loss_share="7"))

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.proposed_cost is None


async def test_improvement_never_raises_the_cost_by_itself(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Стало лучше» не поднимает стоимость автоматически — это решение человека."""
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(
            session, asset_id=asset.id, message="поставили новый компрессор", user_id=None
        )
        await session.commit()

        await process_pending(session, call=_answer(impact_kind="improvement"))

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.proposed_cost is None
        assert Decimal(str(asset.initial_cost)) == Decimal("100000.00")


async def test_model_failure_keeps_the_message_and_does_not_retry(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модель не ответила — сообщение остаётся в истории, повторно не крутится.

    Вызов платный: непонятное сообщение не должно гонять модель в цикле до конца времён.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="Сломался ТЭН", user_id=None)
        await session.commit()

        async def failing(_settings, **_kwargs):
            raise LlmCallError("Anthropic не обслуживает запросы с IP этого сервера (403).")

        await process_pending(session, call=failing)

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.status == "failed"
        assert "403" in (report.error or "")
        assert report.message == "Сломался ТЭН"

        # Второй проход её НЕ берёт: статус уже не 'pending'.
        assert (await process_pending(session, call=failing))["processed"] == 0


async def test_owner_acceptance_moves_the_cost_and_rewrites_the_tail(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Владелец согласился — стоимость меняется так, чтобы остаточная стала предложенной.

    Меняем ПЕРВОНАЧАЛЬНУЮ: остаточная — производная от начислений, отдельно её хранить негде.
    Поэтому новой первоначальной становится «предложенная плюс уже начисленный износ».
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        report = await submit_report(
            session, asset_id=asset.id, message="Отказал компрессор", user_id=None
        )
        await session.commit()
        await process_pending(session, call=_answer())

        await decide_report(session, report=report, accept=True, user_id=None)
        await session.commit()

        assert report.status == "applied"
        assert report.decided_at is not None
        # Износа ещё не было, поэтому первоначальная = предложенной остаточной.
        assert Decimal(str(asset.initial_cost)) == Decimal("20000.00")


async def test_owner_refusal_leaves_the_asset_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отклонение не трогает объект и закрывает запись — второй раз решить нельзя."""
    async with async_session_factory() as session:
        asset = await _asset(session)
        report = await submit_report(
            session, asset_id=asset.id, message="Отказал компрессор", user_id=None
        )
        await session.commit()
        await process_pending(session, call=_answer())

        await decide_report(session, report=report, accept=False, user_id=None)
        await session.commit()

        assert report.status == "dismissed"
        assert Decimal(str(asset.initial_cost)) == Decimal("100000.00")

        with pytest.raises(LlmCallError):
            await decide_report(session, report=report, accept=True, user_id=None)


def test_second_message_while_first_is_pending_is_refused(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Двойное «Сохранить» не должно дать два платных вызова модели по одному поводу."""
    headers = _admin(async_session_factory)
    created = client.post(
        BASE,
        headers=headers,
        json={"name": "Шкаф", "initial_cost": "100000.00", "useful_life_months": 84},
    )
    asset_id = created.json()["id"]

    first = client.post(
        f"{BASE}/{asset_id}/condition", headers=headers, json={"message": "Сломался компрессор"}
    )
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "pending"

    second = client.post(
        f"{BASE}/{asset_id}/condition", headers=headers, json={"message": "И ещё дверь"}
    )
    assert second.status_code == 409

    detail = client.get(f"{BASE}/{asset_id}", headers=headers).json()
    assert len(detail["condition_reports"]) == 1
    assert detail["condition_reports"][0]["cost_before"] == "100000.00"


def test_settings_reuse_the_shared_anthropic_credentials() -> None:
    """Ключ и релей общие на все сервисы — новых заводить не надо, своя только модель."""
    settings = get_settings()
    assert settings.fixed_asset_ai_model
    assert hasattr(settings, "anthropic_relay_secret")
    assert settings.anthropic_timeout_seconds > 0


def _life_answer(**overrides: Any):
    """Ответ модели про ОСТАТОК СРОКА — второй вид обращения (покупка б/у)."""
    payload = {
        "life_used_share": "0.5",
        "reasoning": "Объект 2018 года, отработал примерно половину срока, состояние рабочее.",
        "confidence": 0.7,
        "needs_human": False,
    }
    payload.update(overrides)

    async def call(_settings, **_kwargs) -> dict[str, Any]:
        return payload

    return call


async def test_purchase_prompt_asks_about_life_and_not_about_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У покупки б/у предмет разговора — СРОК, а не стоимость.

    Цена б/у объекта износ уже содержит: продавец его учёл, за поношенное берут меньше. Просить
    у модели ещё и скидку значило бы посчитать износ дважды. А вот срок приходит из категории и
    считает объект новым — это и есть незакрытая дыра, ради которой второй вид обращения заведён.

    Срок категории обязан быть в промпте: доля, которую вернёт модель, берётся именно от него.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, cost="180000.00")
        asset.name = "Пароконвектомат Rational SCC WE 101"
        asset.condition = "used"
        report = await submit_report(
            session,
            asset_id=asset.id,
            message="Куплен б/у. 2018 года, дверь не закрывается плотно",
            user_id=None,
            kind="purchase",
        )
        await session.commit()

        prompt = await build_purchase_prompt(session, report)

    assert report.kind == "purchase"
    assert "Срок службы для НОВОГО объекта этой категории: 84 мес (7 лет)" in prompt
    assert "2018 года, дверь не закрывается плотно" in prompt
    assert "Месяцы и деньги НЕ считай" in prompt


async def test_code_computes_remaining_months_from_the_share(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модель дала долю 0.5 — месяцы считает код: 84 × (1 − 0.5) = 42.

    То же правило, что и с деньгами: дело модели — интерпретация, не арифметика. Стоимость при
    этом не двигается ни на копейку, и срок в карточке ждёт решения владельца.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, cost="180000.00")
        await submit_report(
            session,
            asset_id=asset.id,
            message="Куплен б/у. 2018 года",
            user_id=None,
            kind="purchase",
        )
        await session.commit()

        assert await process_pending(session, call=_life_answer()) == {
            "processed": 1,
            "proposed": 1,
            "failed": 0,
        }
        report = await session.scalar(select(AssetConditionReport))

    assert report is not None
    assert report.proposed_useful_life_months == 42
    # Денег покупка не касается вовсе — иначе износ был бы посчитан дважды.
    assert report.proposed_cost is None
    assert Decimal(str(asset.initial_cost)) == Decimal("180000.00")
    assert asset.useful_life_months == 84, "срок меняет человек, а не джоба"


async def test_worn_out_object_keeps_a_tenth_of_its_life(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доля 1.0 не даёт нулевого срока: за лом денег не платят.

    Карточка с нулевым сроком не амортизируется ВОВСЕ — то есть остаётся на балансе навсегда.
    Это ровно та ошибка, которую контур чинит, только с другого края.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(
            session, asset_id=asset.id, message="Куплен б/у, убитый", user_id=None, kind="purchase"
        )
        await session.commit()

        await process_pending(session, call=_life_answer(life_used_share="1"))
        report = await session.scalar(select(AssetConditionReport))

    # Потолок доли — 0.9, значит объекту остаётся десятая часть срока категории.
    assert report is not None
    assert report.proposed_useful_life_months == 8


async def test_purchase_without_age_or_condition_reaches_the_owner_anyway(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оценить нечем — предложения нет, но запись доходит.

    Молчание было бы хуже пустого предложения: владелец не узнал бы, что объект б/у, и карточка
    осталась бы «как новая» по сроку из категории.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(
            session, asset_id=asset.id, message="Куплен б/у", user_id=None, kind="purchase"
        )
        await session.commit()

        await process_pending(session, call=_life_answer(needs_human=True, reasoning=""))
        report = await session.scalar(select(AssetConditionReport))

    assert report is not None
    assert report.status == "proposed"
    assert report.proposed_useful_life_months is None
    assert report.proposed_reason == "Из описания не понять, сколько объект уже отработал"


async def test_owner_decision_moves_the_term_and_leaves_the_cost_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Применение предложения по покупке ставит СРОК и не трогает стоимость.

    Стоимость и есть уплаченная сумма — менять её после покупки не на чем и незачем.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, cost="180000.00")
        await submit_report(
            session,
            asset_id=asset.id,
            message="Куплен б/у. 2018 года",
            user_id=None,
            kind="purchase",
        )
        await session.commit()
        await process_pending(session, call=_life_answer())

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        await decide_report(session, report=report, accept=True, user_id=None)
        await session.commit()
        await session.refresh(asset)

    assert report.status == "applied"
    assert asset.useful_life_months == 42
    assert Decimal(str(asset.initial_cost)) == Decimal("180000.00")


async def test_breakdown_still_talks_about_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Поломка у работающего объекта осталась разговором о стоимости.

    Проверка сторожит РАЗВИЛКУ: перепутанный вид обращения задал бы модели не тот вопрос, а
    заметить это можно было бы только по странному предложению в карточке.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        report = await submit_report(
            session, asset_id=asset.id, message="Отказал компрессор", user_id=None
        )
        assert report.kind == "incident", "умолчание — поломка, а не покупка"
        await session.commit()

        await process_pending(session, call=_answer())
        stored = await session.scalar(select(AssetConditionReport))

    assert stored is not None
    assert Decimal(str(stored.proposed_cost)) == Decimal("20000.00")
    assert stored.proposed_useful_life_months is None
