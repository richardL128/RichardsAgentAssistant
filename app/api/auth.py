"""Fail-closed authentication boundary for the personal operations console."""

from __future__ import annotations

import secrets
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config import Settings

OPS_CONSOLE_USER_ID = "ops-console"
_basic = HTTPBasic(auto_error=False)


def require_ops_console_user(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str:
    """Authenticate the single console user without exposing configured credentials."""

    settings = cast(Settings, request.app.state.settings)
    username = settings.ops_console_username
    password = settings.ops_console_password
    if username is None or password is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations console authentication is not configured",
        )

    supplied_username = credentials.username if credentials is not None else ""
    supplied_password = credentials.password if credentials is not None else ""
    username_matches = secrets.compare_digest(
        supplied_username.encode(),
        username.get_secret_value().encode(),
    )
    password_matches = secrets.compare_digest(
        supplied_password.encode(),
        password.get_secret_value().encode(),
    )
    if not (username_matches and password_matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operations console credentials",
            headers={"WWW-Authenticate": 'Basic realm="LifeAgent operations", charset="UTF-8"'},
        )
    return OPS_CONSOLE_USER_ID


__all__ = ["OPS_CONSOLE_USER_ID", "require_ops_console_user"]
