from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class LineMessagingApiError(RuntimeError):
    pass


class LineReplyClient:
    def __init__(
        self,
        channel_access_token: str,
        api_base_url: str = "https://api.line.me",
        timeout_sec: float = 10.0,
    ) -> None:
        self.channel_access_token = (channel_access_token or "").strip()
        self.api_base_url = (api_base_url or "https://api.line.me").rstrip("/")
        self.timeout_sec = float(timeout_sec)

    def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        if not self.channel_access_token:
            raise LineMessagingApiError("line_messaging.channel_access_token is required")
        token = (reply_token or "").strip()
        if not token:
            raise LineMessagingApiError("reply token is empty")
        if not messages:
            return

        payload = {"replyToken": token, "messages": messages[:5]}
        self._post_json("/v2/bot/message/reply", payload)

    def push(self, to: str, messages: list[dict[str, Any]]) -> None:
        if not self.channel_access_token:
            raise LineMessagingApiError("line_messaging.channel_access_token is required")
        target = (to or "").strip()
        if not target:
            raise LineMessagingApiError("push target is empty")
        if not messages:
            return

        payload = {"to": target, "messages": messages[:5]}
        self._post_json("/v2/bot/message/push", payload)

    def _post_json(self, path: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.api_base_url}{path}"
        req = request.Request(url=url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", f"Bearer {self.channel_access_token}")
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                if status >= 400:
                    raise LineMessagingApiError(f"line api error: status={status}")
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            raise LineMessagingApiError(f"line api error: status={exc.code} body={body}") from exc
        except error.URLError as exc:
            raise LineMessagingApiError(f"line api connection error: {exc}") from exc

