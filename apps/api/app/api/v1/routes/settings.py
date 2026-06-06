from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.auth.permissions import permission_is_granted
from app.db.session import get_session
from app.schemas.settings import (
    AppSettingHistoryRead,
    AppSettingRead,
    AppSettingUpdate,
    SubstitutePairsRead,
    SubstitutePairsUpdate,
)
from app.services import iiko_sync, payroll_config, settings_service

router = APIRouter()
SETTINGS_READ_ACCESS = (Depends(require_permission("settings.general.read")),)
SETTINGS_EDIT_ACCESS = (Depends(require_permission("settings.general.edit")),)
SOURCE_DEPOSIT_SETTING_KEYS = frozenset(
    {
        "payroll.category_rules",
        "payroll.deposit_auto_withholding_enabled",
    }
)
SOURCE_FUND_SETTING_KEYS = frozenset({"payroll.fund_rates_by_tenure"})
SOURCE_SETTING_READ_KEYS = {
    "source.deposit_settings.read": SOURCE_DEPOSIT_SETTING_KEYS,
    "source.fund_settings.read": SOURCE_FUND_SETTING_KEYS,
}
SOURCE_SETTING_EDIT_KEYS = {
    "source.deposit_settings.edit": SOURCE_DEPOSIT_SETTING_KEYS,
    "source.fund_settings.edit": SOURCE_FUND_SETTING_KEYS,
}


def _not_found(key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Setting '{key}' was not found",
    )


@router.get("", response_model=list[AppSettingRead])
async def list_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    category: Annotated[str | None, Query()] = None,
) -> list[dict]:
    settings = await settings_service.list_settings(session, category)
    if permission_is_granted("settings.general.read", actor.permissions):
        return settings
    allowed_keys = _allowed_setting_keys(actor, write=False)
    if not allowed_keys:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
    return [setting for setting in settings if setting["key"] in allowed_keys]


@router.get(
    "/substitute-pairs",
    response_model=SubstitutePairsRead,
    dependencies=SETTINGS_READ_ACCESS,
)
async def get_substitute_pairs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> SubstitutePairsRead:
    pairs = await payroll_config.get_substitute_pairs(session)
    return SubstitutePairsRead(pairs=[pair.model_dump() for pair in pairs])


@router.put(
    "/substitute-pairs",
    response_model=SubstitutePairsRead,
    dependencies=SETTINGS_EDIT_ACCESS,
)
async def put_substitute_pairs(
    payload: SubstitutePairsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> SubstitutePairsRead:
    try:
        pairs = await payroll_config.set_substitute_pairs(session, payload.pairs, actor)
        await iiko_sync.refresh_role_review_for_all_employees(session, force=True)
    except payroll_config.PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SubstitutePairsRead(pairs=[pair.model_dump() for pair in pairs])


@router.get("/{key}", response_model=AppSettingRead)
async def get_setting(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    _ensure_setting_access(actor, key, write=False)
    try:
        return await settings_service.get_setting(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None


@router.put("/{key}", response_model=AppSettingRead)
async def put_setting(
    key: str,
    payload: AppSettingUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    _ensure_setting_access(actor, key, write=True)
    if settings_service.is_critical_setting_key(key) and not (actor.roles & {"owner", "admin"}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner can change critical settings",
        )

    try:
        return await settings_service.write_setting(session, key, payload.value, actor.user_id)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None
    except settings_service.SettingValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from None


@router.get(
    "/{key}/history",
    response_model=list[AppSettingHistoryRead],
)
async def get_setting_history(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    _ensure_setting_access(actor, key, write=False)
    try:
        return await settings_service.get_setting_history(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None


def _ensure_setting_access(actor: CurrentActor, key: str, *, write: bool) -> None:
    permission = "settings.general.edit" if write else "settings.general.read"
    if permission_is_granted(permission, actor.permissions):
        return
    if key in _allowed_setting_keys(actor, write=write):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")


def _allowed_setting_keys(actor: CurrentActor, *, write: bool) -> set[str]:
    permission_map = SOURCE_SETTING_EDIT_KEYS if write else SOURCE_SETTING_READ_KEYS
    allowed: set[str] = set()
    for permission_code, keys in permission_map.items():
        if permission_is_granted(permission_code, actor.permissions):
            allowed.update(keys)
    return allowed
