"""Реестр должностей: «Настройки → Должности».

Операции и их iiko-семантика:
  - POST / — добавить: создаёт роль в iiko (PUT byCode) и должность в реестре;
  - PATCH /{id} — изменить: обновляет реестр и пушит роль в iiko;
  - POST /{id}/exclude, /{id}/restore — статус только в реестре, iiko не трогаем;
  - DELETE /{id} — удаляет из реестра и DELETE роли в iiko; блокируется, пока
    есть сотрудники с должностью;
  - POST /sync-iiko — линкует существующие должности с ролями iiko и заводит
    неизвестные роли как excluded.
"""

from __future__ import annotations

import http.client as _http_client
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import Position
from app.schemas.positions import (
    PositionCreateRequest,
    PositionIikoSyncResult,
    PositionListRead,
    PositionRead,
    PositionUpdateRequest,
)
from app.services import position_service
from app.services.iiko_sync import (
    IikoEmployeeOperationError,
    delete_iiko_role,
    get_iiko_employee_roles,
    upsert_iiko_role,
)
from app.services.position_registry import (
    normalize_position_name,
    refresh_position_registry,
)
from app.services.position_service import PositionServiceError

router = APIRouter()
POSITIONS_READ_ACCESS = (Depends(require_permission("settings.positions.read")),)
POSITIONS_EDIT_ACCESS = (Depends(require_permission("settings.positions.edit")),)


def _http_error(exc: PositionServiceError | IikoEmployeeOperationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


_IIKO_TIMEOUT_DETAIL = "iiko не отвечает — операция не выполнена. Попробуйте через минуту."


async def _read_positions(session: AsyncSession) -> list[PositionRead]:
    positions = await position_service.list_positions(session)
    counts = await position_service.employee_counts_by_position(session)
    reads = []
    for position in positions:
        read = PositionRead.model_validate(position)
        read.employee_count = counts.get(normalize_position_name(position.name), 0)
        reads.append(read)
    return reads


@router.get("", response_model=PositionListRead, dependencies=POSITIONS_READ_ACCESS)
async def list_positions(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PositionListRead:
    return PositionListRead(positions=await _read_positions(session))


@router.post(
    "",
    response_model=PositionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=POSITIONS_EDIT_ACCESS,
)
async def create_position(
    payload: PositionCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PositionRead:
    name = payload.name.strip()
    if await position_service.find_by_name(session, name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Должность с таким названием уже есть в реестре",
        )
    schedule_type = payload.schedule_type or _default_schedule_type(payload.archetype)

    taken_codes = {
        position.iiko_role_code
        for position in await position_service.list_positions(session)
        if position.iiko_role_code
    }
    role_code = position_service.generate_iiko_role_code(name, taken_codes)
    try:
        iiko_role = await upsert_iiko_role(
            session,
            name=name,
            code=role_code,
            schedule_type=schedule_type,
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(status_code=502, detail=_IIKO_TIMEOUT_DETAIL) from exc
    except IikoEmployeeOperationError as exc:
        raise _http_error(exc) from exc

    try:
        position = await position_service.create_position(
            session,
            name=name,
            archetype=payload.archetype,
            permission_group=payload.permission_group,
            participates_in_access=payload.participates_in_access,
            schedule_type=schedule_type,
            iiko_role_id=iiko_role.role_id,
            iiko_role_code=iiko_role.code,
            actor_user_id=actor.user_id,
        )
    except PositionServiceError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    await refresh_position_registry(session)
    return await _read_single(session, position.id)


@router.patch("/{position_id}", response_model=PositionRead, dependencies=POSITIONS_EDIT_ACCESS)
async def update_position(
    position_id: uuid.UUID,
    payload: PositionUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PositionRead:
    try:
        position = await position_service.get_position(session, position_id)
    except PositionServiceError as exc:
        raise _http_error(exc) from exc

    changes = payload.model_dump(exclude_unset=True)
    if "archetype" in changes and changes.get("schedule_type") is None:
        changes.setdefault("schedule_type", position.schedule_type)
    try:
        position = await position_service.update_position(
            session,
            position,
            changes=changes,
            actor_user_id=actor.user_id,
        )
    except PositionServiceError as exc:
        raise _http_error(exc) from exc

    if position.iiko_role_code:
        try:
            iiko_role = await upsert_iiko_role(
                session,
                name=position.name,
                code=position.iiko_role_code,
                role_id=position.iiko_role_id,
                schedule_type=position.schedule_type,
            )
        except _http_client.IncompleteRead as exc:
            await session.rollback()
            raise HTTPException(status_code=502, detail=_IIKO_TIMEOUT_DETAIL) from exc
        except IikoEmployeeOperationError as exc:
            await session.rollback()
            raise _http_error(exc) from exc
        position.iiko_role_id = iiko_role.role_id
        position.iiko_role_code = iiko_role.code

    await session.commit()
    await refresh_position_registry(session)
    return await _read_single(session, position.id)


@router.post(
    "/{position_id}/exclude",
    response_model=PositionRead,
    dependencies=POSITIONS_EDIT_ACCESS,
)
async def exclude_position(
    position_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PositionRead:
    return await _set_status(session, position_id, "excluded", actor)


@router.post(
    "/{position_id}/restore",
    response_model=PositionRead,
    dependencies=POSITIONS_EDIT_ACCESS,
)
async def restore_position(
    position_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PositionRead:
    return await _set_status(session, position_id, "active", actor)


@router.delete(
    "/{position_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=POSITIONS_EDIT_ACCESS,
)
async def delete_position(
    position_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    try:
        position = await position_service.get_position(session, position_id)
        occupied = await position_service.employees_with_position(session, position.name)
        if occupied:
            raise PositionServiceError(
                f"Нельзя удалить должность «{position.name}»: "
                f"она назначена сотрудникам ({occupied}). Используйте «Исключить».",
                status_code=409,
            )
    except PositionServiceError as exc:
        raise _http_error(exc) from exc

    if position.iiko_role_id:
        try:
            await delete_iiko_role(session, role_id=position.iiko_role_id)
        except _http_client.IncompleteRead as exc:
            raise HTTPException(status_code=502, detail=_IIKO_TIMEOUT_DETAIL) from exc
        except IikoEmployeeOperationError as exc:
            raise _http_error(exc) from exc

    try:
        await position_service.delete_position(session, position, actor_user_id=actor.user_id)
    except PositionServiceError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    await refresh_position_registry(session)


@router.post(
    "/sync-iiko",
    response_model=PositionIikoSyncResult,
    dependencies=POSITIONS_EDIT_ACCESS,
)
async def sync_positions_with_iiko(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PositionIikoSyncResult:
    try:
        roles = await get_iiko_employee_roles(session)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(status_code=502, detail=_IIKO_TIMEOUT_DETAIL) from exc
    except IikoEmployeeOperationError as exc:
        raise _http_error(exc) from exc

    linked, imported = await position_service.link_iiko_roles(
        session, list(roles), actor_user_id=actor.user_id
    )

    # Provision: активным должностям реестра без роли в iiko (Системный
    # администратор, Посудомойка, Уборщица, Старший курьер и т.п.) заводим роль —
    # иначе их нельзя выбрать при создании сотрудника (карточка в iiko требует роль).
    provisioned = 0
    pending = await position_service.active_positions_without_iiko_role(session)
    if pending:
        taken_codes = {
            position.iiko_role_code
            for position in await position_service.list_positions(session)
            if position.iiko_role_code
        }
        for position in pending:
            role_code = position.iiko_role_code or position_service.generate_iiko_role_code(
                position.name, taken_codes
            )
            try:
                iiko_role = await upsert_iiko_role(
                    session,
                    name=position.name,
                    code=role_code,
                    schedule_type=position.schedule_type,
                )
            except _http_client.IncompleteRead as exc:
                raise HTTPException(status_code=502, detail=_IIKO_TIMEOUT_DETAIL) from exc
            except IikoEmployeeOperationError as exc:
                raise _http_error(exc) from exc
            taken_codes.add(iiko_role.code)
            await position_service.attach_iiko_role(
                session,
                position,
                role_id=iiko_role.role_id,
                role_code=iiko_role.code,
                actor_user_id=actor.user_id,
            )
            provisioned += 1

    await session.commit()
    await refresh_position_registry(session)
    return PositionIikoSyncResult(
        linked=linked,
        imported=imported,
        provisioned=provisioned,
        positions=await _read_positions(session),
    )


async def _set_status(
    session: AsyncSession,
    position_id: uuid.UUID,
    new_status: str,
    actor: CurrentActor,
) -> PositionRead:
    try:
        position = await position_service.get_position(session, position_id)
        position = await position_service.set_position_status(
            session, position, status=new_status, actor_user_id=actor.user_id
        )
    except PositionServiceError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    await refresh_position_registry(session)
    return await _read_single(session, position.id)


async def _read_single(session: AsyncSession, position_id: uuid.UUID) -> PositionRead:
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Должность не найдена")
    counts = await position_service.employee_counts_by_position(session)
    read = PositionRead.model_validate(position)
    read.employee_count = counts.get(normalize_position_name(position.name), 0)
    return read


def _default_schedule_type(archetype: str) -> str:
    return "FIXED" if archetype == "okladnik" else "SESSION"
