from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from itsdangerous import URLSafeTimedSerializer

from cv_status import UserRole
from cv_utils import AuthenticationError

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class SessionIdentity:
    username: str
    role: UserRole
    csrf_token: str
    session_version: int


@dataclass(frozen=True)
class MachineIdentity:
    key_id: str
    subject_type: str
    subject_id: str
    scopes: frozenset[str]

    def has_scope(self, required_scope: str) -> bool:
        return required_scope in self.scopes


def hash_password(plaintext: str) -> str:
    return _password_hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, plaintext)
    except Exception:
        return False


def build_session_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt="cv-platform-session")


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def issue_session_token(
    *,
    serializer: URLSafeTimedSerializer,
    username: str,
    role: UserRole,
    csrf_token: str,
    session_version: int,
) -> str:
    return serializer.dumps(
        {
            "username": username,
            "role": role.value,
            "csrf": csrf_token,
            "session_version": session_version,
        }
    )


def read_session_token(
    *,
    serializer: URLSafeTimedSerializer,
    token: str,
    max_age_seconds: int,
) -> SessionIdentity:
    try:
        payload: dict[str, Any] = serializer.loads(token, max_age=max_age_seconds)
    except Exception as exc:
        raise AuthenticationError("Invalid or expired session") from exc
    return SessionIdentity(
        username=str(payload["username"]),
        role=UserRole(str(payload["role"])),
        csrf_token=str(payload["csrf"]),
        session_version=int(payload.get("session_version", 1)),
    )


def hash_machine_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_machine_secret(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_machine_secret(secret), secret_hash)


def encode_machine_token(*, key_id: str, secret: str) -> str:
    return f"{key_id}.{secret}"


def parse_machine_token(
    *,
    authorization_header: str | None = None,
    legacy_api_key_header: str | None = None,
) -> tuple[str, str] | None:
    token_value: str | None = None
    if authorization_header:
        scheme, _, raw_value = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not raw_value.strip():
            raise AuthenticationError("The Authorization header must use the Bearer scheme.")
        token_value = raw_value.strip()
    elif legacy_api_key_header:
        token_value = legacy_api_key_header.strip()

    if not token_value:
        return None

    key_id, separator, secret = token_value.partition(".")
    if not separator or not key_id or not secret:
        raise AuthenticationError(
            "Machine credentials must use the '<key_id>.<secret>' token format."
        )
    return key_id, secret


def parse_scope_string(scope_value: str) -> frozenset[str]:
    return frozenset(part for part in scope_value.split(" ") if part)


def join_scopes(scopes: list[str] | tuple[str, ...] | set[str] | frozenset[str]) -> str:
    return " ".join(sorted({scope for scope in scopes if scope}))
