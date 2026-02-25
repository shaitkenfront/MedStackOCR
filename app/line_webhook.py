from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.config import load_config
from linebot.webhook_handler import LineWebhookHandler

DEFAULT_CONFIG_PATH = "config.yaml"

CONFIG_PATH = os.getenv("LINEBOT_CONFIG_PATH", DEFAULT_CONFIG_PATH)
CONFIG = load_config(CONFIG_PATH)
HANDLER = LineWebhookHandler(CONFIG)

app = FastAPI(title="MedStackOCR LINE Webhook", version="0.1.0")
WEBHOOK_PATH = str(CONFIG.get("line_messaging", {}).get("webhook_path", "/webhook/line"))


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.post(WEBHOOK_PATH)
async def line_webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    status_code, payload = HANDLER.handle(body=body, signature=x_line_signature)
    return JSONResponse(status_code=status_code, content=payload)

