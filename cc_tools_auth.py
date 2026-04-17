"""
Authentication utilities for the CC Tools web platform.

Handles password hashing, JWT session tokens, and password reset tokens.
No secrets are ever exposed through any HTTP response - all validation
happens server-side and returns only boolean pass/fail or opaque tokens.
"""
import os
import secrets
import string
import time
from typing import Optional

import bcrypt
import jwt
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Hash a plaintext password. Plaintext is never stored."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Temp password generation
# ---------------------------------------------------------------------------

def generate_temp_password() -> str:
    """
    Generate a cryptographically secure temporary password.
    Never stored - caller must hash immediately after displaying.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


# ---------------------------------------------------------------------------
# JWT session tokens (stored as httpOnly cookie)
# ---------------------------------------------------------------------------

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_SECONDS = 86400  # 24 hours


def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("JWT_SECRET not set in environment")
    return secret


def create_jwt(user_id: int, email: str, is_admin: bool, force_reset: bool = False) -> str:
    """Create a signed JWT. Store as httpOnly cookie - never expose to JS."""
    payload = {
        "sub": str(user_id),
        "email": email,
        "admin": is_admin,
        "force_reset": force_reset,
        "iat": int(time.time()),
        "exp": int(time.time()) + _JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def decode_jwt(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT. Returns payload dict or None if invalid/expired.
    Never raises - caller treats None as unauthenticated.
    """
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Password reset tokens (itsdangerous, time-limited)
# ---------------------------------------------------------------------------

_RESET_TOKEN_EXPIRY = 3600  # 1 hour


def _reset_serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("JWT_SECRET not set in environment")
    return URLSafeTimedSerializer(secret, salt="password-reset")


def generate_reset_token(email: str) -> str:
    """
    Generate a signed, time-limited reset token for the given email.
    Token is safe to include in a URL - it is not a raw secret.
    """
    return _reset_serializer().dumps(email)


def verify_reset_token(token: str) -> Optional[str]:
    """
    Verify a reset token. Returns the email address if valid, None if expired or invalid.
    Token is valid for 1 hour from generation.
    """
    try:
        return _reset_serializer().loads(token, max_age=_RESET_TOKEN_EXPIRY)
    except (SignatureExpired, BadSignature, Exception):
        return None

# rev 2

# rev 5

# rev 6

# rev 8
