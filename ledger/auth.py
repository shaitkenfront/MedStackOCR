from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

LINE_VERIFY_ID_TOKEN_URL = "https://api.line.me/oauth2/v2.1/verify"

# (url, form) -> (HTTP ステータス, レスポンス本文)。テストで差し替えられるようにする。
HttpPost = Callable[[str, dict[str, str]], tuple[int, str]]


class AuthenticationError(Exception):
    pass


class AuthServiceUnavailableError(Exception):
    """LINE の検証 API に到達できない。利用者の誤りではないので 401 とは分けて扱う。"""


@dataclass(slots=True, frozen=True)
class VerifiedUser:
    line_user_id: str
    expires_at_epoch: int


class LineIdTokenVerifier:
    """LIFF の ID トークンを LINE の検証 API で確かめ、LINE ユーザー ID を取り出す。

    ID トークンの署名・有効期限・発行先（client_id = LINE Login チャネル ID）の検証は LINE 側で行う。
    LINE Login チャネルが Messaging API チャネルと同じプロバイダーにないとユーザー ID が一致しない点に注意。
    """

    def __init__(
        self,
        channel_id: str,
        *,
        http_post: HttpPost | None = None,
        clock: Callable[[], float] = time.time,
        max_cache_entries: int = 1000,
    ) -> None:
        self.channel_id = str(channel_id or "").strip()
        self._http_post = http_post or _urllib_post
        self._clock = clock
        self._max_cache_entries = max(1, int(max_cache_entries))
        self._cache: dict[str, VerifiedUser] = {}
        self._lock = threading.Lock()

    def verify(self, id_token: str) -> VerifiedUser:
        token = str(id_token or "").strip()
        if not token:
            raise AuthenticationError("missing id token")
        if not self.channel_id:
            raise AuthenticationError("LINE Login channel id is not configured")

        cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at_epoch > now:
                return cached

        status, body = self._http_post(
            LINE_VERIFY_ID_TOKEN_URL,
            {"id_token": token, "client_id": self.channel_id},
        )
        if status >= 500 or status == 429:
            raise AuthServiceUnavailableError(f"LINE verify API unavailable (status={status})")
        if status != 200:
            raise AuthenticationError(f"id token rejected by LINE (status={status})")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise AuthenticationError("invalid verify response") from exc

        user_id = str(payload.get("sub") or "").strip()
        audience = str(payload.get("aud") or "").strip()
        expires_at = int(payload.get("exp") or 0)
        if not user_id or audience != self.channel_id or expires_at <= now:
            raise AuthenticationError("id token payload is not acceptable")

        verified = VerifiedUser(line_user_id=user_id, expires_at_epoch=expires_at)
        with self._lock:
            if len(self._cache) >= self._max_cache_entries:
                self._cache = {key: value for key, value in self._cache.items() if value.expires_at_epoch > now}
                if len(self._cache) >= self._max_cache_entries:
                    self._cache.clear()
            self._cache[cache_key] = verified
        return verified


def extract_bearer_token(headers: dict[str, str] | None) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() != "authorization":
            continue
        scheme, _, token = str(value or "").partition(" ")
        if scheme.lower() == "bearer":
            return token.strip()
    return ""


def _urllib_post(url: str, form: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - 固定の https URL
            return int(response.status), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthServiceUnavailableError(str(exc)) from exc
