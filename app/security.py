"""Passwords, login tokens, API keys and encryption. Standard library + cryptography only."""
import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user_id: int, secret: str, ttl_seconds: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":")).encode()
    body = _b64(payload)
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str, secret: str):
    """Return the user id, or None if the token is invalid or expired."""
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(body))
        if int(data["exp"]) < time.time():
            return None
        return int(data["uid"])
    except (ValueError, KeyError, TypeError):
        return None


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_key():
    """Return (full_key, display_prefix, hash). Only the hash is stored."""
    raw = "sk_live_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_api_key(raw)


def _fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("enc:" + secret).encode()).digest()))


def encrypt(text: str, secret: str) -> str:
    return _fernet(secret).encrypt(text.encode()).decode()


def decrypt(token: str, secret: str):
    try:
        return _fernet(secret).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
