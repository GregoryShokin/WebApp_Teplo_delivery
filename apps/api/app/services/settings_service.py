from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, AppSettingHistory, User

CRITICAL_SETTING_KEYS = frozenset(
    {
        "balance_close_deadline",
        "fixed_asset_threshold",
        "repair_vs_modernization_pct",
        "balance.close_day",
        "balance.close_deadline",
        "fixed_assets.capitalization_threshold_rub",
        "fixed_assets.threshold_rub",
        "fixed_assets.repair_modernization_threshold_ratio",
        "fixed_assets.repair_vs_modernization_pct",
    }
)


class SettingNotFoundError(LookupError):
    pass


def is_critical_setting_key(key: str) -> bool:
    return key in CRITICAL_SETTING_KEYS


def _user_name(user: User | None) -> str | None:
    if user is None:
        return None
    return user.full_name or user.email


def serialize_setting(setting: AppSetting, user: User | None = None) -> dict[str, Any]:
    return {
        "id": setting.id,
        "key": setting.key,
        "value": setting.value,
        "value_type": setting.value_type,
        "category": setting.category,
        "description": setting.description,
        "updated_at": setting.updated_at,
        "updated_by_user_id": setting.updated_by_user_id,
        "updated_by_user_name": _user_name(user),
    }


def serialize_history(history: AppSettingHistory, user: User | None = None) -> dict[str, Any]:
    return {
        "id": history.id,
        "setting_id": history.setting_id,
        "old_value": history.old_value,
        "new_value": history.new_value,
        "changed_at": history.changed_at,
        "changed_by_user_id": history.changed_by_user_id,
        "changed_by_user_name": _user_name(user),
    }


async def list_settings(
    session: AsyncSession, category: str | None = None
) -> list[dict[str, Any]]:
    stmt = (
        select(AppSetting, User)
        .outerjoin(User, AppSetting.updated_by_user_id == User.id)
        .order_by(AppSetting.category, AppSetting.key)
    )
    if category:
        stmt = stmt.where(AppSetting.category == category)

    result = await session.execute(stmt)
    return [serialize_setting(setting, user) for setting, user in result.all()]


async def get_setting(session: AsyncSession, key: str) -> dict[str, Any]:
    stmt = (
        select(AppSetting, User)
        .outerjoin(User, AppSetting.updated_by_user_id == User.id)
        .where(AppSetting.key == key)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise SettingNotFoundError(key)

    setting, user = row
    return serialize_setting(setting, user)


async def get_setting_model(session: AsyncSession, key: str) -> AppSetting:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        raise SettingNotFoundError(key)
    return setting


async def write_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    changed_by_user_id: uuid.UUID,
) -> dict[str, Any]:
    setting = await get_setting_model(session, key)
    old_value = setting.value

    setting.value = value
    setting.updated_at = datetime.now(UTC)
    setting.updated_by_user_id = changed_by_user_id
    session.add(
        AppSettingHistory(
            setting_id=setting.id,
            old_value=old_value,
            new_value=value,
            changed_by_user_id=changed_by_user_id,
        )
    )
    await session.commit()
    await session.refresh(setting)

    user = await session.get(User, changed_by_user_id)
    return serialize_setting(setting, user)


async def get_setting_history(session: AsyncSession, key: str) -> list[dict[str, Any]]:
    setting = await get_setting_model(session, key)
    stmt = (
        select(AppSettingHistory, User)
        .outerjoin(User, AppSettingHistory.changed_by_user_id == User.id)
        .where(AppSettingHistory.setting_id == setting.id)
        .order_by(desc(AppSettingHistory.changed_at))
    )
    result = await session.execute(stmt)
    return [serialize_history(history, user) for history, user in result.all()]
