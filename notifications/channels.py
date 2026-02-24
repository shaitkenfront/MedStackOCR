from __future__ import annotations

from typing import Any

from notifications.base import HttpJsonClient, NotificationError

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"


class SlackWebhookNotifier:
    name = "slack"

    def __init__(self, webhook_url: str, http_client: HttpJsonClient) -> None:
        self.webhook_url = webhook_url.strip()
        self.http_client = http_client

    def send(self, message: str) -> None:
        if not self.webhook_url:
            raise NotificationError("slack webhook url is empty")
        self.http_client.post_json(self.webhook_url, {"text": message})


class DiscordWebhookNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, http_client: HttpJsonClient) -> None:
        self.webhook_url = webhook_url.strip()
        self.http_client = http_client

    def send(self, message: str) -> None:
        if not self.webhook_url:
            raise NotificationError("discord webhook url is empty")
        self.http_client.post_json(self.webhook_url, {"content": message})


class LinePushNotifier:
    name = "line"

    def __init__(
        self,
        channel_access_token: str,
        to: str,
        http_client: HttpJsonClient,
    ) -> None:
        self.channel_access_token = channel_access_token.strip()
        self.to = to.strip()
        self.http_client = http_client

    def send(self, message: str) -> None:
        if not self.channel_access_token:
            raise NotificationError("line channel_access_token is empty")
        if not self.to:
            raise NotificationError("line destination `to` is empty")
        payload: dict[str, Any] = {
            "to": self.to,
            "messages": [{"type": "text", "text": message[:5000]}],
        }
        headers = {"Authorization": f"Bearer {self.channel_access_token}"}
        self.http_client.post_json(LINE_PUSH_ENDPOINT, payload, headers=headers)
