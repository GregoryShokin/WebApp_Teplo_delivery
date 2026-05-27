from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.api.v1.routes import auth as auth_routes
from app.auth import CurrentUser
from app.core.security import create_refresh_token
from app.main import create_app


def _user(*roles: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        email="admin@teplo.local",
        full_name="Teplo Admin",
        roles=tuple(roles),
    )


def test_refresh_with_valid_cookie_returns_new_tokens(monkeypatch) -> None:
    app = create_app()
    user = _user("admin")
    refresh_token = create_refresh_token(str(user.id), {"email": user.email})

    async def get_user_by_id(_session, user_id: uuid.UUID):
        assert user_id == user.id
        return object()

    async def build_authenticated_user(_session, _db_user):
        return user

    monkeypatch.setattr(auth_routes, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(auth_routes, "build_authenticated_user", build_authenticated_user)

    with TestClient(app) as client:
        client.cookies.set("teplo_refresh_token", refresh_token, path="/api/v1/auth")
        response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["roles"] == ["admin"]


def test_logout_clears_refresh_cookie() -> None:
    app = create_app()

    with TestClient(app) as client:
        client.cookies.set("teplo_refresh_token", "stale", path="/api/v1/auth")
        response = client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "teplo_refresh_token" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
