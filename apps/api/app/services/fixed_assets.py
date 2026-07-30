"""Амортизация основных средств и правило «ремонт vs модернизация».

Метод один для всех категорий — ЛИНЕЙНЫЙ ПОМЕСЯЧНЫЙ (решение владельца): месячная сумма =
первоначальная стоимость / срок полезного использования в месяцах. Начисление идёт с месяца
ввода в эксплуатацию и прекращается, когда остаточная стоимость дошла до нуля либо объект выбыл.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, AppSetting, AssetCategory, DepreciationEntry, FixedAsset
from app.models.fixed_asset import FIXED_ASSET_THRESHOLD, UPGRADE_SHARE_THRESHOLD
from app.services.taxes.concurrency import insert_or_reread

# Статусы, при которых амортизация не начисляется. ``disposed``/``sold`` — объект выбыл.
# ``not_working`` — методология инвентаризации 2026: неработающая техника стоит на балансе по
# стоимости лома и ждёт утилизации; переносить её стоимость на продукт нечем, потому что она
# ничего не производит. Начнёт работать — статус сменится, и амортизация пойдёт по своей
# категории; поэтому категорию таким карточкам ставим настоящую, а не пустую.
# ``in_storage`` сюда НЕ входит: исправное оборудование в запасе стареет так же, как в работе.
INACTIVE_STATUSES = ("disposed", "sold", "not_working")

# Настройки на странице «Настройки» → «Учёт ОС». Владелец правит их сам, поэтому расчёт
# обязан спрашивать базу, а не константу: иначе цифра в интерфейсе и цифра в расчёте разъедутся.
CAPITALIZATION_THRESHOLD_KEY = "fixed_assets.capitalization_threshold_rub"
UPGRADE_SHARE_KEY = "fixed_assets.repair_modernization_threshold_ratio"

# Серия инвентарных номеров. Сквозная, без кодирования категории или локации: категория
# карточки может смениться, а номер на объекте наклеен один раз и меняться не должен.
INVENTORY_NUMBER_PREFIX = "ОС-"
INVENTORY_NUMBER_WIDTH = 4
# Сколько раз пробуем подобрать свободный номер, если гонка увела предыдущий.
_NUMBER_ATTEMPTS = 5


class FixedAssetError(Exception):
    """Ошибка контура учёта ОС, понятная пользователю."""


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_start(value: date) -> date:
    return value.replace(day=1)


async def _setting_decimal(session: AsyncSession, key: str, default: Decimal) -> Decimal:
    """Числовая настройка из ``app_setting``; отсутствует или испорчена — значение по умолчанию.

    Падать здесь нельзя: пустая настройка не должна ронять начисление амортизации или разбор
    платежа. Умолчание — та же цифра, что в константе модели, поэтому поведение предсказуемо
    и на пустой базе (тесты, свежий стенд).
    """
    raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == key))
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return default


async def capitalization_threshold(session: AsyncSession) -> Decimal:
    """Порог признания основным средством."""
    return await _setting_decimal(session, CAPITALIZATION_THRESHOLD_KEY, FIXED_ASSET_THRESHOLD)


async def upgrade_share_threshold(session: AsyncSession) -> Decimal:
    """Доля от первоначальной стоимости, разделяющая ремонт и модернизацию."""
    return await _setting_decimal(session, UPGRADE_SHARE_KEY, UPGRADE_SHARE_THRESHOLD)


async def is_fixed_asset_purchase(session: AsyncSession, amount: Decimal) -> bool:
    """Покупка признаётся основным средством, а не расходом периода.

    Правило детерминированное, без модели: сравнение с порогом и ничего больше (решение
    владельца 2026-07-30 — классификация по правилам). Граница ВКЛЮЧИТЕЛЬНАЯ: ровно 10 000 ₽
    это уже основное средство.

    Порог сравнивается со стоимостью ЕДИНИЦЫ, а не суммой платежа: семь стульев по 3 000 ₽ —
    это семь вещей по 3 000, а не одна за 21 000. Иначе порог обходится закупкой пачкой.
    """
    return _money(amount) >= await capitalization_threshold(session)


async def resolve_useful_life(session: AsyncSession, asset: FixedAsset) -> int | None:
    """СПИ объекта: собственный срок карточки важнее срока категории."""
    if asset.useful_life_months:
        return asset.useful_life_months
    if asset.category_id is None:
        return None
    return await session.scalar(
        select(AssetCategory.useful_life_months).where(AssetCategory.id == asset.category_id)
    )


async def accumulated_depreciation(session: AsyncSession, asset_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(DepreciationEntry.amount), 0)).where(
            DepreciationEntry.asset_id == asset_id
        )
    )
    return _money(total)


async def residual_value(session: AsyncSession, asset: FixedAsset) -> Decimal:
    """Остаточная стоимость: первоначальная минус накопленная амортизация."""
    accrued = await accumulated_depreciation(session, asset.id)
    return max(_money(asset.initial_cost) - accrued, Decimal("0.00"))


async def accrue_depreciation(
    session: AsyncSession, *, period_month: date, asset_id: uuid.UUID | None = None
) -> list[DepreciationEntry]:
    """Начислить амортизацию за месяц. Идемпотентно: повторный прогон ничего не добавит.

    Пропускаются объекты, которые ещё не введены в эксплуатацию (``commissioned_on`` пуст или
    относится к будущему месяцу), выбывшие, без СПИ и уже самортизированные полностью.

    Последний месяц срока закрывается ОСТАТКОМ, а не расчётной долей: при делении на срок
    копейки округления накапливаются, и без этого у объекта вечно висел бы хвост в несколько
    копеек, который не даёт остаточной стоимости дойти до нуля.
    """
    period = month_start(period_month)
    query = select(FixedAsset).where(FixedAsset.status.not_in(INACTIVE_STATUSES))
    if asset_id is not None:
        query = query.where(FixedAsset.id == asset_id)
    assets = (await session.scalars(query)).all()

    created: list[DepreciationEntry] = []
    for asset in assets:
        if asset.commissioned_on is None or month_start(asset.commissioned_on) > period:
            continue
        life = await resolve_useful_life(session, asset)
        if not life:
            continue

        exists = await session.scalar(
            select(DepreciationEntry.id).where(
                DepreciationEntry.asset_id == asset.id,
                DepreciationEntry.period_month == period,
            )
        )
        if exists is not None:
            continue

        residual = await residual_value(session, asset)
        if residual <= 0:
            continue

        planned = _money(_money(asset.initial_cost) / Decimal(life))
        amount = min(planned, residual) if planned > 0 else residual
        # Хвост меньше планового взноса дотягиваем сразу — иначе остаётся вечная копейка.
        if residual - amount < planned:
            amount = residual

        entry = DepreciationEntry(
            asset_id=asset.id,
            period_month=period,
            amount=amount,
            residual_after=_money(residual - amount),
        )
        # Проверка «строка уже есть» выше — это подсказка, а не защита: между SELECT и INSERT
        # ночная джоба и ручной пересчёт успевают наложиться. Настоящая защита — уникальный
        # индекс, а вложенная транзакция превращает проигранную гонку в «уже начислено»
        # вместо падения всего прогона по 149 объектам.
        stored, is_new = await insert_or_reread(
            session,
            entry,
            reread=lambda asset_id=asset.id: session.scalar(
                select(DepreciationEntry).where(
                    DepreciationEntry.asset_id == asset_id,
                    DepreciationEntry.period_month == period,
                )
            ),
        )
        if is_new:
            created.append(stored)

    await session.flush()
    return created


async def recompute_residuals(session: AsyncSession, asset_id: uuid.UUID) -> None:
    """Пересчитать хранимый остаток по всей истории объекта.

    ``residual_after`` — снимок, а не вычисляемое поле (баланс не должен пересчитывать всю
    историю на каждый показ). Цена снимка: правка суммы за август делает остатки сентября и
    всех следующих месяцев враньём. Поэтому любая правка обязана прогнать хвост заново.

    Идём по ВСЕЙ истории объекта, а не с месяца правки: месяцев у объекта столько же, сколько
    в СПИ (десятки, максимум 120), а полный проход исключает накопленный перекос, если
    какой-то месяц уже был испорчен раньше.
    """
    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        return
    entries = (
        await session.scalars(
            select(DepreciationEntry)
            .where(DepreciationEntry.asset_id == asset_id)
            .order_by(DepreciationEntry.period_month)
        )
    ).all()

    residual = _money(asset.initial_cost)
    for entry in entries:
        residual = _money(residual - _money(entry.amount))
        entry.residual_after = residual
    await session.flush()


async def correct_depreciation(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    period_month: date,
    amount: Decimal,
    user_id: uuid.UUID | None = None,
    note: str | None = None,
) -> DepreciationEntry:
    """Поправить сумму начисления за месяц вручную.

    Коррекция — правка строки месяца, а не сторнирующая проводка: вторую строку за месяц не
    пускает уникальный индекс, а отрицательную сумму — ограничение. Обе защиты оставлены
    сознательно, потому что именно они делают безопасным повторный прогон ночного закрытия.

    Строка помечается ручной. Без этой пометки нельзя отличить ошибку расчёта от осознанной
    правки владельца, а значит нельзя перезапустить закрытие месяца, не затерев решение
    человека.
    """
    period = month_start(period_month)
    entry = await session.scalar(
        select(DepreciationEntry).where(
            DepreciationEntry.asset_id == asset_id,
            DepreciationEntry.period_month == period,
        )
    )
    if entry is None:
        raise FixedAssetError(f"За {period:%m.%Y} по этому объекту начисления нет — править нечего")

    value = _money(amount)
    if value < 0:
        raise FixedAssetError("Сумма амортизации не может быть отрицательной")

    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        raise FixedAssetError("Объект не найден")

    # Больше первоначальной стоимости объект самортизировать не может: иначе остаток уйдёт
    # в минус и баланс покажет отрицательный актив.
    others = await session.scalar(
        select(func.coalesce(func.sum(DepreciationEntry.amount), 0)).where(
            DepreciationEntry.asset_id == asset_id,
            DepreciationEntry.period_month != period,
        )
    )
    ceiling = _money(asset.initial_cost) - _money(others)
    if value > ceiling:
        raise FixedAssetError(
            f"Больше {ceiling:.2f} ₽ за этот месяц начислить нельзя: "
            f"объект самортизируется сверх первоначальной стоимости"
        )

    entry.amount = value
    entry.is_manual = True
    entry.corrected_by_user_id = user_id
    entry.corrected_at = datetime.now(UTC)
    entry.note = note
    await session.flush()
    await recompute_residuals(session, asset_id)
    return entry


async def close_month(
    session: AsyncSession, *, period_month: date, reason: str = "cron"
) -> AgentRun:
    """Закрыть месяц: начислить амортизацию по всем объектам и записать прогон в журнал.

    Прогон логируется в ``agent_run`` — тем же журналом, что и синки. Без него результат
    ночного закрытия жил бы только в логах контейнера, а они ротируются и обнуляются при
    пересборке: «почему за сентябрь начислено на 3 000 меньше» ответить было бы нечем.

    Перезапуск безопасен: начисление идемпотентно по паре «объект и месяц», уже посчитанные
    строки (в том числе поправленные вручную) не трогаются.

    НЕЗАКОНЧЕННЫЙ МЕСЯЦ ЗАКРЫТЬ НЕЛЬЗЯ. Амортизация начисляется за отработанное время, и
    начислить её за месяц, который ещё идёт (тем более за будущий), — значит завысить износ и
    занизить баланс на величину, которой пока не существует. Ошибка при этом тихая: цифра
    выглядит правдоподобно, а «последнее закрытие — август» посреди июля замечает только
    внимательный человек. Ночная джоба закрывает ПРОШЕДШИЙ месяц и в это ограничение не
    упирается; гард нужен ручному запуску, где месяц выбирает пользователь.
    """
    period = month_start(period_month)
    current = month_start(datetime.now(UTC).date())
    if period >= current:
        raise FixedAssetError(
            f"{period:%m.%Y} ещё не закончился — амортизацию начисляют за отработанное время. "
            f"Последний месяц, который можно закрыть, — {(current - timedelta(days=1)):%m.%Y}"
        )
    run = AgentRun(
        agent_name="fixed_assets_month_close",
        status="running",
        params={"period_month": period.isoformat(), "reason": reason},
        result={},
    )
    session.add(run)
    await session.flush()

    try:
        created = await accrue_depreciation(session, period_month=period)
        total = sum((_money(entry.amount) for entry in created), Decimal("0.00"))
        # Сеть-ловушка: платежи по статьям ОС, за которыми не стоит ни один объект. Считаем
        # ИМЕННО здесь — закрытие месяца единственный момент, когда на цифры точно смотрят.
        # Импорт локальный: asset_analytics зависит от этого модуля, и верхнеуровневый
        # импорт замкнул бы круг.
        from app.services.asset_analytics import find_unlinked_asset_payments

        unlinked = await find_unlinked_asset_payments(session, since=period)
        run.status = "success"
        run.finished_at = datetime.now(UTC)
        run.result = {
            "entries": len(created),
            "amount": str(total),
            "unlinked_asset_payments": len(unlinked),
        }
        await session.commit()
        return run
    except Exception as exc:
        await session.rollback()
        # Журнал пишем ОТДЕЛЬНОЙ транзакцией: откат неудачного начисления не должен уносить
        # с собой запись о том, что прогон был и упал.
        session.add(
            AgentRun(
                agent_name="fixed_assets_month_close",
                status="failed",
                params={"period_month": period.isoformat(), "reason": reason},
                result={"error": str(exc)[:500]},
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()
        raise


def classify_asset_expense(
    initial_cost: Decimal, expense_amount: Decimal, *, share_threshold: Decimal | None = None
) -> str:
    """Ремонт, модернизация или решение владельца — по доле расхода от первоначальной стоимости.

    Правило владельца: меньше 15% — ремонт (расход периода), больше 15% — модернизация
    (капитализируется и меняет базу амортизации), РОВНО 15% — спорная зона, решает владелец.

    ``share_threshold`` даёт вызывающему подставить долю из настроек владельца
    (``upgrade_share_threshold``). Функция остаётся синхронной и без сессии: она чистая
    арифметика, и это позволяет проверять правило без базы.
    """
    threshold = UPGRADE_SHARE_THRESHOLD if share_threshold is None else share_threshold
    base = _money(initial_cost)
    if base <= 0:
        return "requires_owner_review"
    # Долю НЕ округляем: 14 999 из 100 000 — это 0,14999, честный ремонт, а любое округление
    # до сотых/тысячных превратило бы его ровно в 15% и увело на разбор владельцу.
    share = _money(expense_amount) / base
    if share == threshold:
        return "requires_owner_review"
    return "upgrade" if share > threshold else "repair"


async def next_inventory_number(session: AsyncSession) -> str:
    """Следующий свободный номер серии «ОС-NNNN».

    Считаем максимум по своей серии и прибавляем единицу. Номера не из серии (наклейки со
    старых описей вроде «Холодильный стол № 2», ручной ввод) в счёт не идут — иначе чужая
    нумерация сдвигала бы нашу.

    Это только ПОДСКАЗКА: единственность держит частичный уникальный индекс из ``0221``, а
    проигранную гонку разруливает ``create_asset``.
    """
    numbers = (
        await session.scalars(
            select(FixedAsset.inventory_number).where(
                FixedAsset.inventory_number.like(f"{INVENTORY_NUMBER_PREFIX}%")
            )
        )
    ).all()
    top = 0
    for raw in numbers:
        tail = (raw or "")[len(INVENTORY_NUMBER_PREFIX) :].strip()
        if tail.isdigit():
            top = max(top, int(tail))
    return f"{INVENTORY_NUMBER_PREFIX}{top + 1:0{INVENTORY_NUMBER_WIDTH}d}"


async def create_asset(
    session: AsyncSession,
    *,
    name: str,
    initial_cost: Decimal,
    category_id: uuid.UUID | None = None,
    valuation_basis: str = "payment",
    valued_on: date | None = None,
    commissioned_on: date | None = None,
    useful_life_months: int | None = None,
    status: str = "in_use",
    location: str | None = None,
    location_id: uuid.UUID | None = None,
    brand_model: str | None = None,
    source_ref: str | None = None,
    inventory_number: str | None = None,
    note: str | None = None,
    review_status: str = "ok",
    review_reason: str | None = None,
) -> FixedAsset:
    """Завести карточку ОС, присвоив инвентарный номер.

    Номер генерируется автоматически (решение владельца 2026-07-30) — на сотрудника его не
    вешаем. Явный ``inventory_number`` перекрывает генератор: так переносятся объекты с уже
    наклеенным номером.

    Гонку разбираем ПЕРЕПОДБОРОМ номера, а не переиспользованием чужой строки: номер здесь не
    личность объекта, а его ярлык. Две карточки, одновременно взявшие «ОС-0007», — это два
    разных предмета, и второму нужен свой номер, а не первая карточка (поэтому общий
    ``taxes.concurrency.insert_or_reread``, который возвращает выигравшую строку, тут не подходит).
    """
    explicit = (inventory_number or "").strip() or None
    attempts = 1 if explicit else _NUMBER_ATTEMPTS

    for _ in range(attempts):
        candidate = explicit or await next_inventory_number(session)
        asset = FixedAsset(
            name=name,
            initial_cost=_money(initial_cost),
            category_id=category_id,
            valuation_basis=valuation_basis,
            valued_on=valued_on,
            commissioned_on=commissioned_on,
            useful_life_months=useful_life_months,
            status=status,
            location=location,
            location_id=location_id,
            brand_model=brand_model,
            source_ref=source_ref,
            inventory_number=candidate,
            note=note,
            review_status=review_status,
            review_reason=review_reason,
        )
        try:
            # SAVEPOINT: конфликт по номеру не должен отравлять внешнюю транзакцию —
            # заливка реестра на 149 карточек не имеет права упасть целиком из-за одной.
            async with session.begin_nested():
                session.add(asset)
                await session.flush()
        except IntegrityError:
            if asset in session:
                session.expunge(asset)
            if explicit:
                raise FixedAssetError(
                    f"Инвентарный номер «{candidate}» уже занят другой карточкой"
                ) from None
            continue
        return asset

    raise FixedAssetError("Не удалось подобрать свободный инвентарный номер — попробуйте ещё раз")


async def capitalize_upgrade(
    session: AsyncSession, *, asset: FixedAsset, amount: Decimal
) -> FixedAsset:
    """Модернизация увеличивает первоначальную стоимость — дальше амортизируется новая база.

    Уже начисленную амортизацию не пересчитываем задним числом: прошлые месяцы закрыты, а
    остаточная стоимость честно вырастет на сумму модернизации.
    """
    if amount <= 0:
        raise FixedAssetError("Сумма модернизации должна быть положительной")
    asset.initial_cost = _money(asset.initial_cost) + _money(amount)
    await session.flush()
    return asset
