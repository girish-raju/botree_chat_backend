"""Password hashing using the `bcrypt` library directly.

We intentionally avoid `passlib` here: passlib 1.7.4's bcrypt backend probes
`bcrypt.__about__.__version__`, which was removed in bcrypt>=4.1, making the
combination broken. Calling `bcrypt` directly sidesteps that entirely.
"""

from __future__ import annotations

import bcrypt

_ENCODING = "utf-8"


def hash_password(plain: str) -> str:
    """Hash a plaintext password, returning a UTF-8 string suitable for storage."""
    hashed = bcrypt.hashpw(plain.encode(_ENCODING), bcrypt.gensalt())
    return hashed.decode(_ENCODING)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(_ENCODING), hashed.encode(_ENCODING))
    except (ValueError, TypeError):
        return False


__all__ = ["hash_password", "verify_password"]
