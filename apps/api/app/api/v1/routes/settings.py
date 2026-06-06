from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
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


def _not_found(key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Setting '{key}' was not found",
    )


@router.get("", response_model=list[AppSettingRead], dependencies=SETTINGS_READ_ACCESS)
async def list_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    category: Annotated[str | None, Query()] = None,
) -> list[dict]:
    return await settings_service.list_settings(session, category)


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


@router.get("/{key}", response_model=AppSettingRead, dependencies=SETTINGS_READ_ACCESS)
async def get_setting(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        return await settings_service.get_setting(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None


@router.put("/{key}", response_model=AppSettingRead, dependencies=SETTINGS_EDIT_ACCESS)
async def put_setting(
    key: str,
    payload: AppSettingUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
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
    dependencies=SETTINGS_READ_ACCESS,
)
async def get_setting_history(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    try:
        return await settings_service.get_setting_history(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None
