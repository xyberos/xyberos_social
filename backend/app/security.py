import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

password_hasher = PasswordHasher()
DUMMY_PASSWORD_HASH = password_hasher.hash("not-a-real-account-password")
_rate_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=14)


def check_rate_limit(key: str, action: str, limit: int = 10, seconds: int = 900) -> bool:
    now = time.monotonic()
    bucket = (key, action)
    with _rate_lock:
        attempts = _rate_windows[bucket]
        while attempts and now - attempts[0] >= seconds:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
    return True
