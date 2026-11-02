from __future__ import annotations

import base64
import hashlib
import hmac


def verify_line_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    secret = (channel_secret or "").strip()
    received = (signature or "").strip()
    if not secret or not received:
        return False

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, received)

