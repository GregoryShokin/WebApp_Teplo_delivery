"""Выбытие основного средства: объекта больше нет, остаток уходит в убыток.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПЕРЕОЦЕНКИ. Переоценка отвечает на вопрос «сколько объект теперь стоит» —
он остаётся на балансе, просто дешевле. Выбытие отвечает на другой: объекта НЕТ. Украли,
утратили, уничтожен. Актив уходит из внеоборотных целиком, а его остаточная стоимость
становится расходом — убытком от выбытия.

ПОЧЕМУ НЕЛЬЗЯ БЫЛО ОБОЙТИСЬ ПЕРЕОЦЕНКОЙ В НОЛЬ. Применение переоценки считает новую
первоначальную стоимость как «предложенная плюс накопленный износ»: для нуля это переписало бы
первоначальную в размер износа. Карточка соврала бы о цене покупки, акт списания стало бы не из
чего собрать, а объект остался бы в реестре с нулевой строкой вместо того, чтобы из него выйти.

ПРОДАЖА СЮДА НЕ ВХОДИТ. По методологии владельца продажа ОС финрезультата в P&L не даёт — это
перевод внеоборотного актива в деньги, и убытка там нет. Поэтому статус ``sold`` этот контур
не ставит и объект в статусе ``sold`` не трогает.

АМОРТИЗАЦИЯ ЗА МЕСЯЦ ВЫБЫТИЯ НЕ НАЧИСЛЯЕТСЯ. Списанный объект сразу попадает в
``INACTIVE_STATUSES``, и закрытие месяца его пропустит. Значит весь остаток на дату списания
уходит убытком, а не долями «немного амортизации, немного убытка». Так проще и честнее: объект
не работал этот месяц, переносить его стоимость на продукт было нечем.

ЗАКРЫТЫЙ МЕСЯЦ НЕ ТРОГАЕМ. Выбытие в уже замороженном месяце переписало бы строку баланса,
которую владелец перенёс в отчётность. Такой запрос отклоняется: убыток признаётся тем месяцем,
когда о пропаже узнали, — это и есть обычная практика для закрытого периода.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetBalanceSnapshot, AssetConditionReport, AssetMovement, FixedAsset
from app.services.fixed_assets import FixedAssetError, month_start, residual_value

# Тип движения, которым записывается выбытие. Строка одна на объект — держится частичным
# уникальным индексом ``uq_asset_movement_writeoff``.
WRITEOFF = "writeoff"

# Продажа. Второй способ покинуть внеоборотные активы, и от списания он отличается смыслом:
# убытка нет, есть обмен актива на деньги. Уникального индекса у него нет (он стоит только на
# ``writeoff``), поэтому вторую строку продажи ловит проверка в ``sell_asset``.
SALE = "sale"

# Оба вида движения, после которых объекта в балансе больше нет.
GONE_MOVEMENTS = (WRITEOFF, SALE)

# Статусы, из которых списывать нечего: объект уже выбыл.
GONE_STATUSES = ("disposed", "sold")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


async def disposal_for_asset(session: AsyncSession, asset_id: uuid.UUID) -> AssetMovement | None:
    """Строка выбытия объекта, если он списан."""
    return await session.scalar(
        select(AssetMovement).where(
            AssetMovement.asset_id == asset_id,
            AssetMovement.movement_type == WRITEOFF,
        )
    )


async def month_is_frozen(session: AsyncSession, period: date) -> bool:
    """Месяц заморожен снимком баланса — значит его цифры уже перенесены в отчётность.

    Один ответ на один вопрос: им пользуются и запрет списания в закрытый месяц, и карточка
    выбытия, которая гасит кнопку отмены.
    """
    frozen = await session.scalar(
        select(func.count())
        .select_from(AssetBalanceSnapshot)
        .where(AssetBalanceSnapshot.period_month == period)
    )
    return bool(frozen)


async def dispose_asset(
    session: AsyncSession,
    *,
    asset: FixedAsset,
    reason: str,
    disposed_on: date | None = None,
    user_id: uuid.UUID | None = None,
    condition_report_id: uuid.UUID | None = None,
) -> AssetMovement:
    """Списать объект: статус ``disposed`` и строка выбытия с суммой убытка.

    Первоначальную стоимость и историю начислений НЕ трогаем — карточка остаётся историческим
    документом: за сколько купили, сколько износа успели начислить, сколько осталось на момент
    пропажи. Именно эти три числа и нужны акту списания.
    """
    today = datetime.now(UTC).date()
    when = disposed_on or today

    # Строку выбытия проверяем ВСЕГДА, а не только у выбывшего по статусу: статус карточки
    # правится отдельным маршрутом, и «объект в работе, а строка выбытия есть» — состояние,
    # которое обязано упереться в понятную ошибку, а не в нарушение уникального индекса.
    existing = await disposal_for_asset(session, asset.id)
    if existing is not None:
        raise FixedAssetError(f"Объект уже списан {existing.occurred_on:%d.%m.%Y}")
    if asset.status in GONE_STATUSES:
        raise FixedAssetError("Объект уже выбыл из учёта — списывать нечего")

    if when > today:
        raise FixedAssetError("Дата выбытия не может быть в будущем")

    if asset.commissioned_on is not None and when < asset.commissioned_on:
        raise FixedAssetError(
            f"Объект введён в эксплуатацию {asset.commissioned_on:%d.%m.%Y} — "
            f"выбыть раньше он не мог"
        )

    period = month_start(when)
    if await month_is_frozen(session, period):
        raise FixedAssetError(
            f"{period:%m.%Y} уже закрыт и перенесён в отчётность — оформите выбытие "
            f"месяцем, когда о пропаже узнали"
        )

    residual = await residual_value(session, asset)

    movement = AssetMovement(
        asset_id=asset.id,
        movement_type=WRITEOFF,
        occurred_on=when,
        # Убыток — остаточная стоимость на дату выбытия. Хранится своим числом, а не считается
        # заново при показе: карточку и начисления ещё будут править, а цифра, перенесённая в
        # ОПиУ, поехать за ними не должна.
        amount=_money(residual),
        # Только причина, без цифр: сумма лежит в ``amount``, и склеивать её в текст значило бы
        # хранить одно число дважды. Для выбытия по сообщению менеджера здесь его слова.
        note=reason.strip(),
        previous_status=asset.status,
        condition_report_id=condition_report_id,
        created_by_user_id=user_id,
    )
    session.add(movement)
    asset.status = "disposed"
    await session.flush()
    return movement


async def sell_asset(
    session: AsyncSession,
    *,
    asset: FixedAsset,
    sold_on: date | None = None,
    amount: Decimal | None = None,
    note: str | None = None,
    user_id: uuid.UUID | None = None,
) -> AssetMovement:
    """Продать объект: актив уходит из внеоборотных, убытка нет.

    ПОЧЕМУ ОТДЕЛЬНО ОТ СПИСАНИЯ. По методологии владельца продажа ОС финрезультата в P&L не
    даёт — это перевод внеоборотного актива в деньги. Поэтому здесь не считается остаточная
    стоимость как убыток и строка ОПиУ «УчОС Убыток от выбытия» не растёт. ``amount`` — цена
    продажи, справочная: деньги придут своим путём, через ДДС.

    ПОЧЕМУ ВООБЩЕ ПОЯВИЛСЯ МАРШРУТ. Раньше продажа оформлялась правкой статуса в карточке, и
    это был единственный путь. Он обходил строку движения, а без неё баланс не знает МЕСЯЦА
    выбытия: объект исчезал сразу из всех прошлых месяцев, включая замороженные и уже
    перенесённые в отчётность. Запретить правку статуса, не дав взамен маршрут, значило бы
    сделать продажу неоформляемой вовсе.
    """
    today = datetime.now(UTC).date()
    when = sold_on or today

    existing = await gone_movement_for_asset(session, asset.id)
    if existing is not None:
        raise FixedAssetError(
            f"Объект уже выбыл из учёта {existing.occurred_on:%d.%m.%Y} — повторно продать нельзя"
        )
    if asset.status in GONE_STATUSES:
        raise FixedAssetError("Объект уже выбыл из учёта — продавать нечего")

    if when > today:
        raise FixedAssetError("Дата продажи не может быть в будущем")

    if asset.commissioned_on is not None and when < asset.commissioned_on:
        raise FixedAssetError(
            f"Объект введён в эксплуатацию {asset.commissioned_on:%d.%m.%Y} — "
            f"продан раньше он быть не мог"
        )

    period = month_start(when)
    if await month_is_frozen(session, period):
        raise FixedAssetError(
            f"{period:%m.%Y} уже закрыт и перенесён в отчётность — оформите продажу месяцем, "
            f"в котором о ней узнали"
        )

    movement = AssetMovement(
        asset_id=asset.id,
        movement_type=SALE,
        occurred_on=when,
        amount=_money(amount) if amount is not None else None,
        note=(note or "").strip() or None,
        previous_status=asset.status,
        created_by_user_id=user_id,
    )
    session.add(movement)
    asset.status = "sold"
    await session.flush()
    return movement


async def gone_movement_for_asset(
    session: AsyncSession, asset_id: uuid.UUID
) -> AssetMovement | None:
    """Строка, которой объект покинул баланс, — списание или продажа."""
    return await session.scalar(
        select(AssetMovement)
        .where(
            AssetMovement.asset_id == asset_id,
            AssetMovement.movement_type.in_(GONE_MOVEMENTS),
        )
        .order_by(AssetMovement.occurred_on)
    )


async def cancel_disposal(session: AsyncSession, *, asset: FixedAsset) -> None:
    """Отменить ошибочное списание: вернуть статус и убрать убыток из ОПиУ.

    Кнопка нужна ровно потому, что списание необратимо на глаз: объект исчезает из реестра, из
    свода и из строк баланса разом. Без отмены единственным способом починить промах остаётся
    правка базы руками.

    Замороженный месяц не отменяем по той же причине, по которой в него не списываем: цифра
    уже перенесена в отчётность.

    Строку выбытия УДАЛЯЕМ, а не помечаем отменённой: пока она есть, частичный уникальный
    индекс не даст списать объект повторно, а «отменённое выбытие» в журнале жизни объекта
    пришлось бы исключать из каждого запроса про убыток. Кто отменил — не пишем никуда по той
    же причине: писать некуда, строки больше нет.
    """
    movement = await disposal_for_asset(session, asset.id)
    if movement is None:
        raise FixedAssetError("У объекта нет оформленного выбытия")

    period = month_start(movement.occurred_on)
    if await month_is_frozen(session, period):
        raise FixedAssetError(
            f"{period:%m.%Y} уже закрыт и перенесён в отчётность — отменить выбытие в нём нельзя"
        )

    # Сообщение о состоянии, из которого выросло решение, возвращаем в «ждёт решения»: иначе
    # в истории объекта осталось бы «применено» рядом с отменённым списанием.
    if movement.condition_report_id is not None:
        report = await session.get(AssetConditionReport, movement.condition_report_id)
        if report is not None and report.status == "applied":
            report.status = "proposed"
            report.decided_at = None
            report.decided_by_user_id = None

    asset.status = movement.previous_status or "in_use"
    await session.delete(movement)
    await session.flush()


async def disposal_series(
    session: AsyncSession, *, date_from: date | None = None, date_to: date | None = None
) -> list[tuple[date, Decimal, int]]:
    """Помесячный ряд убытка от выбытия — строка ОПиУ «УчОС Убыток от выбытия».

    Ряд-близнец помесячной амортизации: обе цифры переносятся в отчёт о прибылях руками, и
    обе обязаны приходить из модуля готовыми. Считать убыток на глаз по разнице реестров
    невозможно — списанный объект из реестра исчезает.

    Группируем в питоне, а не в SQL: выбытий за год единицы, а ``date_trunc`` по колонке-дате
    пришлось бы приводить типами ради экономии, которой на таком объёме не существует.
    """
    rows = (
        await session.execute(
            select(AssetMovement.occurred_on, AssetMovement.amount)
            .where(AssetMovement.movement_type == WRITEOFF)
            .order_by(AssetMovement.occurred_on)
        )
    ).all()

    buckets: dict[date, tuple[Decimal, int]] = {}
    for occurred_on, amount in rows:
        period = month_start(occurred_on)
        if date_from is not None and period < month_start(date_from):
            continue
        if date_to is not None and period > month_start(date_to):
            continue
        total, count = buckets.get(period, (Decimal("0.00"), 0))
        buckets[period] = (_money(total + _money(amount)), count + 1)
    return [(period, total, count) for period, (total, count) in sorted(buckets.items())]
