from __future__ import annotations

from typing import Any
from urllib import error, request


class LineMediaApiError(RuntimeError):
    pass


class LineMediaApiClient:
    def __init__(
        self,
        channel_access_token: str,
        data_api_base_url: str = "https://api-data.line.me",
        timeout_sec: float = 10.0,
    ) -> None:
        self.channel_access_token = (channel_access_token or "").strip()
        self.data_api_base_url = (data_api_base_url or "https://api-data.line.me").rstrip("/")
        self.timeout_sec = float(timeout_sec)

    def download_message_content(self, message_id: str) -> tuple[bytes, str | None]:
        if not self.channel_access_token:
            raise LineMediaApiError("line_messaging.channel_access_token is required")
        msg_id = (message_id or "").strip()
        if not msg_id:
            raise LineMediaApiError("message id is empty")

        url = f"{self.data_api_base_url}/v2/bot/message/{msg_id}/content"
        req = request.Request(url=url, method="GET")
        req.add_header("Authorization", f"Bearer {self.channel_access_token}")
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                content = resp.read()
                content_type = resp.headers.get("Content-Type")
                return content, content_type
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            raise LineMediaApiError(f"line media api error: status={exc.code} body={body}") from exc
        except error.URLError as exc:
            raise LineMediaApiError(f"line media api connection error: {exc}") from exc


def guess_extension(content_type: str | None, fallback: str = ".jpg") -> str:
    normalized = str(content_type or "").lower()
    if "png" in normalized:
        return ".png"
    if "jpeg" in normalized or "jpg" in normalized:
        return ".jpg"
    if "gif" in normalized:
        return ".gif"
    if "webp" in normalized:
        return ".webp"
    return fallback

