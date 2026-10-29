"""Bearer token authentication."""

from __future__ import annotations

from .config import Principal, hash_token


class AuthError(Exception):
    """Raised when a request has no usable credentials. The message is for logs only."""


def authenticate(authorization: str | None, tokens: dict[str, Principal]) -> Principal:
    if not authorization:
        raise AuthError("missing Authorization header")
    scheme, _, credentials = authorization.partition(" ")
    credentials = credentials.strip()
    if scheme.lower() != "bearer" or not credentials:
        raise AuthError("Authorization header is not a Bearer token")
    principal = tokens.get(hash_token(credentials))
    if principal is None:
        raise AuthError("unknown token")
    return principal
