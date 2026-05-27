from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user, require_role
from app.db.session import get_session
from app.schemas.settings import AppSettingHistoryRead, AppSettingRead, AppSettingUpdate
from app.services import settings_service

router = APIRouter()


def _not_found(key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Setting '{key}' was not found",
    )


@router.get("", response_model=list[AppSettingRead])
async def list_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
    category: Annotated[str | None, Query()] = None,
) -> list[dict]:
    return await settings_service.list_settings(session, category)


@router.get("/{key}", response_model=AppSettingRead)
async def get_setting(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> dict:
    try:
        return await settings_service.get_setting(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None


@router.put("/{key}", response_model=AppSettingRead)
async def put_setting(
    key: str,
    payload: AppSettingUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[CurrentUser, Depends(require_role("finance_manager", "owner"))],
) -> dict:
    if settings_service.is_critical_setting_key(key) and "owner" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner can change critical settings",
        )

    try:
        return await settings_service.write_setting(session, key, payload.value, user.id)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None


@router.get("/{key}/history", response_model=list[AppSettingHistoryRead])
async def get_setting_history(
    key: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[CurrentUser, Depends(current_user)],
) -> list[dict]:
    try:
        return await settings_service.get_setting_history(session, key)
    except settings_service.SettingNotFoundError:
        raise _not_found(key) from None
