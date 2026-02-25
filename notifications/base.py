from __future__ import annotations

import json
from typing import Any, Protocol
from urllib import error, request


class NotificationError(RuntimeError):
    pass


class NotificationChannel(Protocol):
    name: str

    def send(self, message: str) -> None:
        ...


class HttpJsonClient(Protocol):
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        ...


class UrllibHttpJsonClient:
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                if status >= 400:
                    raise NotificationError(f"notification request failed: status={status}")
        except error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise NotificationError(f"notification request failed: status={exc.code} body={body}") from exc
        except error.URLError as exc:
            raise NotificationError(f"notification request failed: {exc}") from exc
