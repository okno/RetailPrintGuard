"""Argon2/JWT authentication and bounded login throttling."""

from __future__ import annotations

import hashlib
import secrets
from collections import OrderedDict, deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from retailprintguard.api.schemas import RoleName, UserPrincipal

ISSUER = "retailprintguard"
AUDIENCE = "retailprintguard-api"
_DUMMY_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)
_DUMMY_HASH = _DUMMY_HASHER.hash(secrets.token_urlsafe(48))


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True


class TokenService:
    def __init__(self, secret: bytes, *, lifetime_minutes: int) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes")
        self._secret = secret
        self._lifetime = timedelta(minutes=lifetime_minutes)

    @property
    def expires_in(self) -> int:
        return int(self._lifetime.total_seconds())

    def issue(self, user: UserPrincipal) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": str(user.id),
                "username": user.username,
                "roles": [role.value for role in user.roles],
                "iat": now,
                "nbf": now,
                "exp": now + self._lifetime,
                "jti": hashlib.sha256(f"{user.id}:{now.timestamp()}".encode()).hexdigest(),
            },
            self._secret,
            algorithm="HS256",
        )

    def decode(self, token: str) -> UserPrincipal:
        claims = jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "nbf", "sub", "roles"]},
        )
        roles = tuple(RoleName(value) for value in claims["roles"])
        return UserPrincipal(
            id=UUID(claims["sub"]),
            username=str(claims["username"]),
            roles=roles,
            active=True,
        )


class LoginThrottle:
    """Bounded process-local throttle; reverse proxy limits remain mandatory."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int = 300,
        delay_seconds: float = 2,
        maximum_buckets: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._delay = delay_seconds
        if maximum_buckets < 2:
            raise ValueError("maximum_buckets must be at least 2")
        self._maximum_buckets = maximum_buckets
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _keys(client_ip: str, username: str) -> tuple[str, str]:
        ip_key = hashlib.sha256(f"ip\0{client_ip}".encode()).hexdigest()
        account_key = hashlib.sha256(f"account\0{username.casefold()}".encode()).hexdigest()
        return ip_key, account_key

    def _bucket(self, key: str) -> deque[float]:
        bucket = self._events.get(key)
        if bucket is None:
            while len(self._events) >= self._maximum_buckets:
                self._events.popitem(last=False)
            bucket = deque(maxlen=self._limit * 2)
            self._events[key] = bucket
        else:
            self._events.move_to_end(key)
        return bucket

    def retry_after(self, client_ip: str, username: str) -> float:
        now = monotonic()
        with self._lock:
            result = 0.0
            for key in self._keys(client_ip, username):
                events = self._bucket(key)
                while events and now - events[0] > self._window:
                    events.popleft()
                if len(events) >= self._limit:
                    result = max(result, self._delay, self._window - (now - events[0]))
            return result

    def failure(self, client_ip: str, username: str) -> None:
        with self._lock:
            now = monotonic()
            for key in self._keys(client_ip, username):
                self._bucket(key).append(now)

    def success(self, client_ip: str, username: str) -> None:
        with self._lock:
            # A successful account clears its account bucket.  The IP bucket is
            # retained so rotating usernames cannot bypass the network limit.
            self._events.pop(self._keys(client_ip, username)[1], None)


def constant_time_dummy_verify(password: str) -> None:
    """Reduce username enumeration differences when a repository has no user."""

    with suppress(VerifyMismatchError):
        _DUMMY_HASHER.verify(_DUMMY_HASH, password)
