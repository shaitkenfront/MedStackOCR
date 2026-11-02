from __future__ import annotations

import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HANDLER_PATH = Path(__file__).resolve().parents[1] / "infra" / "cdk" / "rich_menu" / "handler.py"
IMAGE_PATH = HANDLER_PATH.parent / "rich_menu.png"


def load_handler():
    spec = importlib.util.spec_from_file_location("rich_menu_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class FakeLineApi:
    def __init__(self, existing: list[dict[str, str]]) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.existing = existing
        self.fail_first_create = False

    def urlopen(self, request, timeout=None):  # noqa: ANN001
        method, url = request.get_method(), request.full_url
        self.calls.append((method, url, request.data, dict(request.header_items())))
        path = url.split("/v2/bot")[-1]
        if method == "POST" and path == "/richmenu" and self.fail_first_create:
            self.fail_first_create = False
            raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b"busy"))  # type: ignore[arg-type]
        if method == "POST" and path == "/richmenu":
            body = {"richMenuId": "richmenu-new"}
        elif method == "GET" and path == "/richmenu/list":
            body = {"richmenus": self.existing + [{"richMenuId": "richmenu-new", "name": "medstackocr-main-v2"}]}
        else:
            body = {}
        response = mock.MagicMock()
        response.read.return_value = json.dumps(body).encode("utf-8")
        response.__enter__.return_value = response
        return response


class RichMenuHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = load_handler()
        secrets = mock.Mock()
        secrets.get_secret_value.return_value = {"SecretString": json.dumps({"line_channel_access_token": "TOKEN"})}
        self.boto = mock.patch.object(self.handler.boto3, "client", return_value=secrets)
        self.boto.start()
        self.handler.time.sleep = lambda _: None

    def tearDown(self) -> None:
        self.boto.stop()

    def run_event(self, api: FakeLineApi, event: dict) -> dict:
        with mock.patch.object(self.handler.urllib.request, "urlopen", side_effect=api.urlopen):
            return self.handler.on_event(event, None)

    def test_image_matches_menu_size(self) -> None:
        from PIL import Image

        with Image.open(IMAGE_PATH) as image:
            self.assertEqual(image.size, (2500, 843))
        self.assertLess(IMAGE_PATH.stat().st_size, 1024 * 1024)  # LINE の上限は 1MB

    def test_create_registers_default_menu_and_removes_only_old_own_menus(self) -> None:
        api = FakeLineApi(
            [
                {"richMenuId": "richmenu-old", "name": "medstackocr-main-v1"},
                {"richMenuId": "richmenu-manual", "name": "OA Manager で作ったメニュー"},
            ]
        )
        api.fail_first_create = True  # 一時的な 503 は再試行する
        result = self.run_event(
            api,
            {"RequestType": "Create", "ResourceProperties": {"SecretId": "s", "LiffId": "LIFF-1", "Version": "v2"}},
        )
        self.assertEqual(result["PhysicalResourceId"], "richmenu-new")
        steps = [(method, url.split("/v2/bot")[-1]) for method, url, _, _ in api.calls]
        self.assertEqual(
            steps,
            [
                ("POST", "/richmenu/validate"),
                ("POST", "/richmenu"),
                ("POST", "/richmenu"),
                ("POST", "/richmenu/richmenu-new/content"),
                ("POST", "/user/all/richmenu/richmenu-new"),
                ("GET", "/richmenu/list"),
                ("DELETE", "/richmenu/richmenu-old"),
            ],
        )
        menu = json.loads(api.calls[1][2])
        self.assertEqual([area["action"]["label"] for area in menu["areas"]], ["明細を見る", "今年の医療費", "申告資料", "使い方"])
        self.assertEqual(menu["areas"][0]["action"]["uri"], "https://liff.line.me/LIFF-1")
        self.assertEqual(sum(area["bounds"]["width"] for area in menu["areas"]), 2500)
        upload = api.calls[3]
        self.assertTrue(upload[1].startswith("https://api-data.line.me/"))
        self.assertEqual(upload[2], IMAGE_PATH.read_bytes())
        headers = {key.lower(): value for key, value in upload[3].items()}
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["authorization"], "Bearer TOKEN")

    def test_delete_removes_only_own_menu(self) -> None:
        api = FakeLineApi([])
        self.run_event(api, {"RequestType": "Delete", "PhysicalResourceId": "richmenu-new", "ResourceProperties": {"SecretId": "s"}})
        self.assertEqual([(m, u.split("/v2/bot")[-1]) for m, u, _, _ in api.calls], [("DELETE", "/richmenu/richmenu-new")])
        api = FakeLineApi([])
        self.run_event(api, {"RequestType": "Delete", "PhysicalResourceId": "failed-create", "ResourceProperties": {"SecretId": "s"}})
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
