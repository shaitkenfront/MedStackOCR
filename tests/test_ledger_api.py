from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from core.enums import FieldName
from inbox.repository import InboxRepository
from ledger.api import JST, ApiRequest, LedgerApi
from ledger.auth import AuthServiceUnavailableError, LineIdTokenVerifier, extract_bearer_token
from ledger.service import LedgerService

CHANNEL_ID = "1650000000"
NOW_EPOCH = 1_780_000_000.0


class FakeLineVerifyApi:
    """LINE の ID トークン検証 API の代わり。token -> userId の対応だけを持つ。"""

    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.calls = 0
        self.unavailable = False

    def __call__(self, url: str, form: dict[str, str]) -> tuple[int, str]:
        self.calls += 1
        if self.unavailable:
            raise AuthServiceUnavailableError("down")
        user_id = self.tokens.get(form["id_token"])
        if user_id is None or form["client_id"] != CHANNEL_ID:
            return 400, json.dumps({"error": "invalid_request"})
        return 200, json.dumps({"sub": user_id, "aud": CHANNEL_ID, "exp": int(NOW_EPOCH) + 600})


class LedgerApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InboxRepository(str(Path(self._tmp.name) / "linebot.db"))
        self.line_api = FakeLineVerifyApi({"token-u1": "U1", "token-u2": "U2"})
        verifier = LineIdTokenVerifier(CHANNEL_ID, http_post=self.line_api, clock=lambda: NOW_EPOCH)
        self.deleted_images: list[str] = []
        self.api = LedgerApi(
            LedgerService(self.repo),
            verifier,
            image_deleter=self.deleted_images.append,
            now=lambda: datetime(2026, 11, 2, tzinfo=JST),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, *, token: str | None = "token-u1", body: Any = None, query: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        raw_body = body if isinstance(body, str) or body is None else json.dumps(body, ensure_ascii=False)
        response = self.api.handle(ApiRequest(method=method, path=path, query=query or {}, headers=headers, body=raw_body))
        return response.status, response.body

    def add_ocr_entry(self, receipt_id: str = "R1") -> None:
        with self.repo._connect() as conn:
            conn.execute(
                """
                INSERT INTO receipts(receipt_id, line_user_id, line_message_id, image_path, image_sha256,
                                     decision_status, decision_confidence, created_at, updated_at)
                VALUES(?, 'U1', 'M1', ?, 'sha', 'AUTO_ACCEPT', 0.9, '2026-03-10T00:00:00+00:00', '2026-03-10T00:00:00+00:00')
                """,
                (receipt_id, f"s3://bucket/{receipt_id}.jpg"),
            )
            conn.commit()
        self.repo.upsert_aggregate_entry(
            receipt_id=receipt_id,
            line_user_id="U1",
            fields={
                FieldName.PAYMENT_DATE: "2026-03-10",
                FieldName.PAYMENT_AMOUNT: 3200,
                FieldName.PAYER_FACILITY_NAME: "さくら薬局",
                FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
            },
            status="confirmed",
        )

    def test_requires_valid_token(self) -> None:
        self.assertEqual(self.call("GET", "/api/entries", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/entries", token="forged")[0], 401)

    def test_auth_service_down_returns_503(self) -> None:
        self.line_api.unavailable = True
        status, body = self.call("GET", "/api/entries")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "auth_unavailable")

    def test_token_verification_is_cached(self) -> None:
        self.call("GET", "/api/entries")
        self.call("GET", "/api/checks")
        self.assertEqual(self.line_api.calls, 1)

    def test_manual_entry_lifecycle(self) -> None:
        status, body = self.call(
            "POST",
            "/api/entries",
            body={"service_date": "2026-04-02", "provider_name": "通院タクシー", "family_member_name": "山田 花子", "amount_yen": 1800},
        )
        self.assertEqual(status, 201)
        entry_id = body["entry"]["entry_id"]
        self.assertNotIn("line_user_id", body["entry"])

        status, body = self.call("PATCH", f"/api/entries/{entry_id}", body={"amount_yen": "2,000", "note": "往復"})
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["amount_yen"], 2000)

        status, body = self.call("GET", "/api/entries", query={"year": "2026"})
        self.assertEqual(status, 200)
        self.assertEqual([item["entry_id"] for item in body["entries"]], [entry_id])
        self.assertEqual(body["summary"]["total_amount_yen"], 2000)

        status, body = self.call("DELETE", f"/api/entries/{entry_id}")
        self.assertEqual((status, body["deleted"]), (200, True))
        self.assertEqual(self.call("GET", f"/api/entries/{entry_id}")[0], 404)

    def test_validation_error_lists_fields(self) -> None:
        status, body = self.call("POST", "/api/entries", body={"amount_yen": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("amount_yen", body["details"]["fields"])
        self.assertIn("provider_name", body["details"]["fields"])

    def test_user_cannot_touch_other_users_entry(self) -> None:
        self.add_ocr_entry()
        self.assertEqual(self.call("GET", "/api/entries/R1", token="token-u2")[0], 404)
        self.assertEqual(self.call("PATCH", "/api/entries/R1", token="token-u2", body={"amount_yen": 1})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/entries/R1", token="token-u2")[0], 404)
        status, body = self.call("GET", "/api/entries", token="token-u2", query={"year": "2026"})
        self.assertEqual((status, body["entries"]), (200, []))
        self.assertEqual(self.deleted_images, [])

    def test_delete_ocr_entry_removes_image(self) -> None:
        self.add_ocr_entry()
        status, _ = self.call("DELETE", "/api/entries/R1")
        self.assertEqual(status, 200)
        self.assertEqual(self.deleted_images, ["s3://bucket/R1.jpg"])

    def test_checks_and_meta(self) -> None:
        self.repo.upsert_family_member("U1", "山田 太郎", ["ヤマダ タロウ"])
        self.add_ocr_entry("R1")
        self.add_ocr_entry("R2")
        status, body = self.call("GET", "/api/checks", query={"year": "2026"})
        self.assertEqual(status, 200)
        self.assertEqual({issue["code"] for issue in body["issues"]}, {"duplicate_candidate"})
        self.assertTrue(body["ready_for_export"])

        status, body = self.call("GET", "/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(body["years"], [2026])
        self.assertEqual(body["current_year"], 2026)
        self.assertEqual(body["family_members"], ["山田 太郎"])
        self.assertIn("その他の医療費", body["medical_categories"])

    def test_bad_requests(self) -> None:
        self.assertEqual(self.call("GET", "/api/entries", query={"year": "26"})[0], 400)
        self.assertEqual(self.call("POST", "/api/entries", body="{broken")[0], 400)
        self.assertEqual(self.call("POST", "/api/entries", body="[1]")[0], 400)
        self.assertEqual(self.call("POST", "/api/entries", body="x" * 20000)[0], 413)
        self.assertEqual(self.call("PUT", "/api/entries")[0], 405)
        self.assertEqual(self.call("GET", "/api/unknown")[0], 404)
        self.assertEqual(self.call("GET", "/api/entries/bad id")[0], 404)

    def test_from_http_api_event(self) -> None:
        event = {
            "rawPath": "/api/entries",
            "requestContext": {"http": {"method": "post"}},
            "headers": {"Authorization": "Bearer abc"},
            "queryStringParameters": {"year": "2026"},
            "body": base64.b64encode("{\"a\": 1}".encode()).decode(),
            "isBase64Encoded": True,
        }
        request = ApiRequest.from_http_api_event(event)
        self.assertEqual((request.method, request.path, request.body), ("POST", "/api/entries", "{\"a\": 1}"))
        self.assertEqual(extract_bearer_token(request.headers), "abc")
        self.assertEqual(request.query, {"year": "2026"})
        self.assertEqual(extract_bearer_token({"X-Line-Id-Token": "xyz", "Authorization": "Bearer abc"}), "xyz")
        self.assertEqual(extract_bearer_token({"Authorization": "Basic abc"}), "")

    def test_verifier_rejects_wrong_audience_and_expired(self) -> None:
        def wrong_aud(url: str, form: dict[str, str]) -> tuple[int, str]:
            return 200, json.dumps({"sub": "U1", "aud": "other", "exp": int(NOW_EPOCH) + 60})

        def expired(url: str, form: dict[str, str]) -> tuple[int, str]:
            return 200, json.dumps({"sub": "U1", "aud": CHANNEL_ID, "exp": int(NOW_EPOCH) - 1})

        from ledger.auth import AuthenticationError

        for http_post in (wrong_aud, expired):
            verifier = LineIdTokenVerifier(CHANNEL_ID, http_post=http_post, clock=lambda: NOW_EPOCH)
            with self.assertRaises(AuthenticationError):
                verifier.verify("t")
        with self.assertRaises(AuthenticationError):
            LineIdTokenVerifier("", http_post=wrong_aud).verify("t")


if __name__ == "__main__":
    unittest.main()
