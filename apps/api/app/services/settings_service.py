from __future__ import annotations

import re
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

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MONTH_DAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_WIDGET_TYPES = frozenset(
    {"text", "number", "percent", "date", "date_array", "select", "boolean", "json", "time"}
)


class SettingNotFoundError(LookupError):
    pass


class SettingValidationError(ValueError):
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
        "display_name": setting.display_name,
        "description": setting.description,
        "widget_type": setting.widget_type,
        "widget_options": setting.widget_options,
        "unit": setting.unit,
        "is_critical": is_critical_setting_key(setting.key),
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
    validate_setting_value(setting, value)
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


def validate_setting_value(setting: AppSetting, value: Any) -> None:
    widget_type = setting.widget_type
    if widget_type not in ALLOWED_WIDGET_TYPES:
        raise SettingValidationError(f"Unsupported widget type: {widget_type}")

    if widget_type == "json":
        return
    if widget_type == "boolean":
        _require(isinstance(value, bool), "Expected boolean value")
        return
    if widget_type == "number":
        _require(_is_number(value), "Expected number value")
        return
    if widget_type == "percent":
        _validate_percent(setting, value)
        return
    if widget_type == "select":
        _validate_select(setting, value)
        return
    if widget_type == "date":
        _require(isinstance(value, str), "Expected date string")
        _require(_is_date_value(setting, value), "Expected date in configured format")
        return
    if widget_type == "time":
        _require(isinstance(value, str) and TIME_RE.match(value), "Expected HH:MM time")
        return
    if widget_type == "date_array":
        _validate_date_array(setting, value)
        return

    _require(isinstance(value, str), "Expected text value")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SettingValidationError(message)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _validate_percent(setting: AppSetting, value: Any) -> None:
    target = value
    value_path = _widget_options(setting).get("value_path")
    if value_path:
        _require(isinstance(value, dict), "Expected object value")
        target = _get_path(value, str(value_path))

    _require(_is_number(target), "Expected numeric percent value")
    _require(0 <= float(target) <= 1, "Expected percent value between 0 and 1")


def _validate_select(setting: AppSetting, value: Any) -> None:
    options = _widget_options(setting).get("options")
    _require(isinstance(options, list) and len(options) > 0, "Select options are not configured")
    option_values = [
        option.get("value")
        for option in options
        if isinstance(option, dict) and "value" in option
    ]
    _require(
        any(value == option_value for option_value in option_values),
        "Value is not in options",
    )


def _validate_date_array(setting: AppSetting, value: Any) -> None:
    if isinstance(value, list):
        dates = value
        ranges: list[Any] = []
    elif isinstance(value, dict):
        dates = value.get("dates")
        ranges = value.get("ranges", [])
    else:
        raise SettingValidationError("Expected date array value")

    _require(isinstance(dates, list), "Expected dates list")
    for date_value in dates:
        _require(isinstance(date_value, str), "Expected date string")
        _require(_is_date_value(setting, date_value), "Expected date in configured format")

    _require(isinstance(ranges, list), "Expected ranges list")
    for range_value in ranges:
        _require(isinstance(range_value, dict), "Expected range object")
        start = range_value.get("start")
        end = range_value.get("end")
        _require(isinstance(start, str) and isinstance(end, str), "Expected range dates")
        _require(_is_date_value(setting, start), "Expected range start in configured format")
        _require(_is_date_value(setting, end), "Expected range end in configured format")


def _is_date_value(setting: AppSetting, value: str) -> bool:
    options = _widget_options(setting)
    if options.get("format") == "MM-DD" or options.get("fixed_year") is False:
        return bool(MONTH_DAY_RE.match(value))
    return bool(ISO_DATE_RE.match(value))


def _widget_options(setting: AppSetting) -> dict[str, Any]:
    return setting.widget_options if isinstance(setting.widget_options, dict) else {}


def _get_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise SettingValidationError(f"Missing value path: {path}")
        current = current[part]
    return current
