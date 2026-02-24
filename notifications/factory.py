from __future__ import annotations

from typing import Any

from notifications.base import HttpJsonClient, NotificationChannel, UrllibHttpJsonClient
from notifications.channels import DiscordWebhookNotifier, LinePushNotifier, SlackWebhookNotifier


def build_notification_channels(
    config: dict[str, Any],
    http_client: HttpJsonClient | None = None,
) -> tuple[dict[str, NotificationChannel], dict[str, str]]:
    nconf = config.get("notifications", {})
    selected = nconf.get("channels", [])
    if not isinstance(selected, list):
        selected = []

    client = http_client or UrllibHttpJsonClient()
    channels: dict[str, NotificationChannel] = {}
    errors: dict[str, str] = {}

    for raw in selected:
        name = str(raw).strip().lower()
        if not name:
            continue
        if name in channels or name in errors:
            continue

        if name == "slack":
            sconf = nconf.get("slack", {})
            webhook = _str_from_dict(sconf, "webhook_url")
            if not webhook:
                errors[name] = "notifications.slack.webhook_url is required"
                continue
            channels[name] = SlackWebhookNotifier(webhook_url=webhook, http_client=client)
            continue

        if name == "discord":
            dconf = nconf.get("discord", {})
            webhook = _str_from_dict(dconf, "webhook_url")
            if not webhook:
                errors[name] = "notifications.discord.webhook_url is required"
                continue
            channels[name] = DiscordWebhookNotifier(webhook_url=webhook, http_client=client)
            continue

        if name == "line":
            lconf = nconf.get("line", {})
            token = _str_from_dict(lconf, "channel_access_token")
            to = _str_from_dict(lconf, "to")
            if not token or not to:
                errors[name] = "notifications.line.channel_access_token and notifications.line.to are required"
                continue
            channels[name] = LinePushNotifier(channel_access_token=token, to=to, http_client=client)
            continue

        errors[name] = f"unsupported notification channel: {name}"

    return channels, errors


def _str_from_dict(value: Any, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get(key, "")).strip()
