from __future__ import annotations

from fastapi import APIRouter

from app.integrations.registry import list_integration_definitions

router = APIRouter()


@router.get("/definitions")
async def integration_definitions() -> list[dict[str, str]]:
    return [definition.model_dump() for definition in list_integration_definitions()]
