"""LINE のリッチメニューを登録する CloudFormation カスタムリソース。

- Create / Update: メニューを作り、画像を載せ、全利用者の既定メニューにする。
  このスタックが以前作ったメニュー（名前が NAME_PREFIX で始まるもの）は削除する。
- Delete: このリソースが作ったメニューを削除する（ほかのメニューには触れない）。

チャネルアクセストークンは Secrets Manager から読み、ログには出さない。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3  # Lambda のランタイムに含まれる

API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
NAME_PREFIX = "medstackocr-main"
IMAGE_PATH = Path(__file__).resolve().parent / "rich_menu.png"
MENU_WIDTH, MENU_HEIGHT = 2500, 843


def menu_areas(liff_id: str) -> list[dict[str, Any]]:
    """画像（make_image.py）の横4分割と同じ順に並べる。"""
    actions = [
        {"type": "uri", "label": "明細を見る", "uri": f"https://liff.line.me/{liff_id}"},
        {"type": "message", "label": "今年の医療費", "text": "今年の医療費"},
        {"type": "message", "label": "申告資料", "text": "申告資料"},
        {"type": "message", "label": "使い方", "text": "ヘルプ"},
    ]
    width = MENU_WIDTH // len(actions)
    return [
        {"bounds": {"x": index * width, "y": 0, "width": width, "height": MENU_HEIGHT}, "action": action}
        for index, action in enumerate(actions)
    ]


def on_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_type = event["RequestType"]
    props = event.get("ResourceProperties") or {}
    token = _load_token(str(props["SecretId"]))

    if request_type == "Delete":
        menu_id = str(event.get("PhysicalResourceId") or "")
        if menu_id.startswith("richmenu-"):
            _request("DELETE", f"{API}/richmenu/{menu_id}", token, allow_not_found=True)
        return {"PhysicalResourceId": menu_id}

    liff_id = str(props["LiffId"])
    name = f"{NAME_PREFIX}-{props.get('Version', 'v1')}"[:300]
    body = {
        "size": {"width": MENU_WIDTH, "height": MENU_HEIGHT},
        "selected": True,
        "name": name,
        "chatBarText": "メニュー",
        "areas": menu_areas(liff_id),
    }
    _request("POST", f"{API}/richmenu/validate", token, json_body=body)
    menu_id = _request("POST", f"{API}/richmenu", token, json_body=body)["richMenuId"]
    _request(
        "POST",
        f"{DATA_API}/richmenu/{menu_id}/content",
        token,
        raw_body=IMAGE_PATH.read_bytes(),
        content_type="image/png",
    )
    _request("POST", f"{API}/user/all/richmenu/{menu_id}", token)

    for menu in _request("GET", f"{API}/richmenu/list", token).get("richmenus", []):
        other_id = str(menu.get("richMenuId") or "")
        if other_id != menu_id and str(menu.get("name") or "").startswith(NAME_PREFIX):
            _request("DELETE", f"{API}/richmenu/{other_id}", token, allow_not_found=True)
    print(f"rich menu registered: {menu_id} ({name})")
    return {"PhysicalResourceId": menu_id, "Data": {"RichMenuId": menu_id}}


def _load_token(secret_id: str) -> str:
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    token = str(json.loads(secret["SecretString"]).get("line_channel_access_token") or "").strip()
    if not token:
        raise RuntimeError("line_channel_access_token is missing in the app secret")
    return token


def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Any = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    allow_not_found: bool = False,
    attempts: int = 3,
) -> dict[str, Any]:
    data = raw_body
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - 固定の LINE API
                text = response.read().decode("utf-8")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_not_found:
                return {}
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code >= 500 or exc.code == 429:
                if attempt < attempts:
                    time.sleep(2 * attempt)
                    continue
            raise RuntimeError(f"LINE API {method} {url.split('/v2/bot')[-1]} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError:
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise
    return {}

