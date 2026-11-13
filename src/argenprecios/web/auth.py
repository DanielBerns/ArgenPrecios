"""Administrator authentication (bcrypt work factor 12), signed cookies, Bearer tokens, & CSRF."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

import bcrypt
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_analytics import AdminUserModel


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    """Verified administrator session or API token identity."""

    username: str
    csrf_token: str
    via_bearer_token: bool = False


def hash_password(plain_password: str, rounds: int = 12) -> str:
    """Hash an administrator password with bcrypt work factor 12."""
    if len(plain_password) < 8:
        raise ValueError("Administrator password must be at least 8 characters long")
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt(rounds=rounds))
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify plain_password against a bcrypt password hash."""
    try:
        return bool(bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8")))
    except ValueError:
        return False


def hash_api_token(raw_token: str) -> str:
    """Compute SHA-256 hex digest of an API Bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_or_update_admin_user(
    session: Session,
    username: str,
    plain_password: str,
    generate_api_token: bool = True,
) -> tuple[AdminUserModel, str | None]:
    """Bootstrap or rotate an administrator user and optional API token."""
    clean_user = username.strip()
    if not clean_user:
        raise ValueError("Username cannot be empty")
    pw_hash = hash_password(plain_password)
    raw_token = f"ap_{secrets.token_urlsafe(32)}" if generate_api_token else None
    tok_hash = hash_api_token(raw_token) if raw_token else None

    existing = session.scalar(select(AdminUserModel).where(AdminUserModel.username == clean_user))
    if existing is None:
        user = AdminUserModel(
            username=clean_user[:80],
            password_hash=pw_hash,
            api_token_hash=tok_hash,
            is_active=True,
        )
        session.add(user)
    else:
        user = existing
        user.password_hash = pw_hash
        user.api_token_hash = tok_hash
        user.is_active = True

    session.commit()
    return (user, raw_token)


class SessionManager:
    """Signs and validates tamper-proof admin session cookies and CSRF tokens."""

    def __init__(
        self,
        secret_key: str,
        cookie_name: str = "argenprecios_session",
        max_age_seconds: int = 43_200,
    ) -> None:
        self.cookie_name = cookie_name
        self.max_age_seconds = max_age_seconds
        self._serializer = URLSafeTimedSerializer(secret_key, salt="argenprecios.admin.v1")

    def create_session_cookie_value(self, username: str) -> tuple[str, str]:
        """Return `(signed_cookie_value, csrf_token)`."""
        csrf = secrets.token_hex(24)
        signed = self._serializer.dumps({"u": username, "csrf": csrf})
        return (signed, csrf)

    def read_session_cookie(self, cookie_value: str | None) -> AuthenticatedAdmin | None:
        if not cookie_value:
            return None
        try:
            payload = self._serializer.loads(cookie_value, max_age=self.max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict) or "u" not in payload or "csrf" not in payload:
            return None
        return AuthenticatedAdmin(
            username=str(payload["u"]),
            csrf_token=str(payload["csrf"]),
            via_bearer_token=False,
        )


def resolve_optional_admin(
    request: Request,
    session: Session,
) -> AuthenticatedAdmin | None:
    """Inspect Authorization Bearer header or signed session cookie."""
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        raw_tok = auth_header[7:].strip()
        if raw_tok:
            tok_hash = hash_api_token(raw_tok)
            user = session.scalar(
                select(AdminUserModel).where(
                    AdminUserModel.api_token_hash == tok_hash,
                    AdminUserModel.is_active.is_(True),
                )
            )
            if user is not None:
                return AuthenticatedAdmin(
                    username=user.username,
                    csrf_token="",
                    via_bearer_token=True,
                )

    manager: SessionManager = request.app.state.session_manager
    raw_cookie = request.cookies.get(manager.cookie_name)
    parsed = manager.read_session_cookie(raw_cookie)
    if parsed is None:
        return None

    active_user = session.scalar(
        select(AdminUserModel).where(
            AdminUserModel.username == parsed.username,
            AdminUserModel.is_active.is_(True),
        )
    )
    return parsed if active_user is not None else None


async def require_authenticated_mutation(
    request: Request,
    session: Session,
) -> AuthenticatedAdmin:
    """Enforce admin auth AND CSRF verification on mutating POST/PUT/PATCH/DELETE requests."""
    admin = resolve_optional_admin(request, session)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Administrator authentication required for Catalog Editor / Basket mutations.",
        )

    if admin.via_bearer_token:
        return admin

    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied_csrf = request.headers.get("X-CSRF-Token") or ""
        if not supplied_csrf:
            content_type = (request.headers.get("content-type") or "").lower()
            if "form" in content_type:
                form_data = await request.form()
                raw_field = form_data.get("csrf_token")
                if isinstance(raw_field, str):
                    supplied_csrf = raw_field

        if not supplied_csrf or not hmac.compare_digest(supplied_csrf, admin.csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing CSRF token.",
            )

    return admin


__all__ = [
    "AuthenticatedAdmin",
    "SessionManager",
    "create_or_update_admin_user",
    "hash_api_token",
    "hash_password",
    "require_authenticated_mutation",
    "resolve_optional_admin",
    "verify_password",
]
