from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ledger.auth import (
    AuthenticationError,
    AuthServiceUnavailableError,
    LineIdTokenVerifier,
    extract_bearer_token,
)
from ledger.models import MEDICAL_CATEGORIES, LedgerEntry
from ledger.service import LedgerEntryNotFoundError, LedgerService, summarize_entries
from ledger.validation import LedgerValidationError

logger = logging.getLogger(__name__)

API_PREFIX = "/api"
MAX_BODY_BYTES = 16 * 1024
MIN_YEAR = 2000
MAX_YEAR = 2100
JST = timezone(timedelta(hours=9))

_ENTRY_PATH_RE = re.compile(r"^/entries/(?P<entry_id>[A-Za-z0-9_\-]{1,100})$")

# 画像パスを受け取り、原本画像を消す。消せなくても明細の削除自体は成功として扱う。
ImageDeleter = Callable[[str], None]


@dataclass(slots=True)
class ApiRequest:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @classmethod
    def from_http_api_event(cls, event: dict[str, Any]) -> "ApiRequest":
        """API Gateway HTTP API（ペイロード形式 2.0）のイベントから作る。"""
        http = (event.get("requestContext") or {}).get("http") or {}
        body = event.get("body")
        if body is not None and event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return cls(
            method=str(http.get("method") or event.get("httpMethod") or "GET").upper(),
            path=str(event.get("rawPath") or http.get("path") or "/"),
            query={str(k): str(v) for k, v in (event.get("queryStringParameters") or {}).items()},
            headers={str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()},
            body=body,
        )


@dataclass(slots=True)
class ApiResponse:
    status: int
    body: dict[str, Any]

    def to_http_api_response(self) -> dict[str, Any]:
        return {
            "statusCode": self.status,
            "headers": {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            "body": json.dumps(self.body, ensure_ascii=False),
        }


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class LedgerApi:
    """LIFF 画面向けの明細 API。利用者は ID トークンからだけ特定し、リクエストの値は信用しない。"""

    def __init__(
        self,
        service: LedgerService,
        verifier: LineIdTokenVerifier,
        *,
        image_deleter: ImageDeleter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(JST),
    ) -> None:
        self.service = service
        self.verifier = verifier
        self.image_deleter = image_deleter
        self._now = now

    def handle(self, request: ApiRequest) -> ApiResponse:
        try:
            path = self._relative_path(request.path)
            user_id = self._authenticate(request)
            return self._dispatch(request, path, user_id)
        except _HttpError as exc:
            body: dict[str, Any] = {"error": exc.code, "message": exc.message}
            if exc.details:
                body["details"] = exc.details
            return ApiResponse(exc.status, body)
        except Exception:  # pragma: no cover - 想定外の失敗は内容を返さずログに残す
            logger.exception("ledger api failed: %s %s", request.method, request.path)
            return ApiResponse(500, {"error": "internal_error", "message": "処理に失敗しました。時間をおいて再度お試しください。"})

    def _dispatch(self, request: ApiRequest, path: str, user_id: str) -> ApiResponse:
        method = request.method
        if path == "/meta":
            self._require_method(method, {"GET"})
            return self._meta(user_id)
        if path == "/entries":
            self._require_method(method, {"GET", "POST"})
            if method == "GET":
                return self._list_entries(user_id, self._year(request))
            return self._create_entry(user_id, self._json_body(request))
        if path == "/checks":
            self._require_method(method, {"GET"})
            report = self.service.check(user_id, self._year(request))
            return ApiResponse(200, report.to_dict())
        match = _ENTRY_PATH_RE.match(path)
        if match is not None:
            self._require_method(method, {"GET", "PATCH", "DELETE"})
            entry_id = match.group("entry_id")
            if method == "GET":
                return ApiResponse(200, {"entry": self._get_entry(user_id, entry_id).to_dict()})
            if method == "PATCH":
                return self._update_entry(user_id, entry_id, self._json_body(request))
            return self._delete_entry(user_id, entry_id)
        raise _HttpError(404, "not_found", "指定されたAPIはありません。")

    # --- handlers ---

    def _meta(self, user_id: str) -> ApiResponse:
        rows = self.service.repository.list_ledger_rows(user_id)
        years = sorted(
            {
                int(reference[:4])
                for reference in (
                    LedgerEntry.from_row(row).effective_date() or str(row.get("created_at") or "")[:10]
                    for row in rows
                )
                if reference[:4].isdigit()
            },
            reverse=True,
        )
        members = self.service.repository.list_family_members(user_id)
        return ApiResponse(
            200,
            {
                "current_year": self._now().year,
                "years": years,
                "medical_categories": list(MEDICAL_CATEGORIES),
                "family_members": [str(member.get("canonical_name")) for member in members if member.get("canonical_name")],
            },
        )

    def _list_entries(self, user_id: str, year: int) -> ApiResponse:
        entries = self.service.list_entries(user_id, year)
        return ApiResponse(
            200,
            {
                "year": year,
                "entries": [entry.to_dict() for entry in entries],
                "summary": summarize_entries(entries, year).to_dict(),
            },
        )

    def _create_entry(self, user_id: str, body: dict[str, Any]) -> ApiResponse:
        try:
            entry = self.service.create_manual_entry(user_id, body)
        except LedgerValidationError as exc:
            raise _validation_error(exc) from exc
        return ApiResponse(201, {"entry": entry.to_dict()})

    def _get_entry(self, user_id: str, entry_id: str) -> LedgerEntry:
        try:
            return self.service.get_entry(user_id, entry_id)
        except LedgerEntryNotFoundError as exc:
            raise _not_found() from exc

    def _update_entry(self, user_id: str, entry_id: str, body: dict[str, Any]) -> ApiResponse:
        try:
            entry = self.service.update_entry(user_id, entry_id, body)
        except LedgerEntryNotFoundError as exc:
            raise _not_found() from exc
        except LedgerValidationError as exc:
            raise _validation_error(exc) from exc
        return ApiResponse(200, {"entry": entry.to_dict()})

    def _delete_entry(self, user_id: str, entry_id: str) -> ApiResponse:
        try:
            result = self.service.delete_entry(user_id, entry_id)
        except LedgerEntryNotFoundError as exc:
            raise _not_found() from exc
        if result.image_path and self.image_deleter is not None:
            try:
                self.image_deleter(result.image_path)
            except Exception:
                logger.exception("failed to delete receipt image for entry %s", entry_id)
        return ApiResponse(200, {"deleted": result.deleted, "entry_id": entry_id})

    # --- helpers ---

    def _authenticate(self, request: ApiRequest) -> str:
        token = extract_bearer_token(request.headers)
        if not token:
            raise _HttpError(401, "unauthorized", "LINEでログインし直してください。")
        try:
            return self.verifier.verify(token).line_user_id
        except AuthenticationError as exc:
            raise _HttpError(401, "unauthorized", "LINEでログインし直してください。") from exc
        except AuthServiceUnavailableError as exc:
            logger.warning("LINE id token verification unavailable: %s", exc)
            raise _HttpError(503, "auth_unavailable", "LINEの認証サービスに接続できません。時間をおいて再度お試しください。") from exc

    @staticmethod
    def _relative_path(path: str) -> str:
        normalized = "/" + str(path or "").strip("/")
        if normalized == API_PREFIX or not normalized.startswith(API_PREFIX + "/"):
            raise _HttpError(404, "not_found", "指定されたAPIはありません。")
        return normalized[len(API_PREFIX):]

    @staticmethod
    def _require_method(method: str, allowed: set[str]) -> None:
        if method not in allowed:
            raise _HttpError(405, "method_not_allowed", "この操作はできません。")

    def _year(self, request: ApiRequest) -> int:
        raw = str(request.query.get("year") or "").strip()
        if not raw:
            return self._now().year
        if not raw.isdigit() or not (MIN_YEAR <= int(raw) <= MAX_YEAR):
            raise _HttpError(400, "invalid_year", "年の指定が正しくありません。")
        return int(raw)

    @staticmethod
    def _json_body(request: ApiRequest) -> dict[str, Any]:
        raw = request.body or ""
        if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
            raise _HttpError(413, "payload_too_large", "入力内容が大きすぎます。")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            raise _HttpError(400, "invalid_json", "入力内容の形式が正しくありません。") from exc
        if not isinstance(body, dict):
            raise _HttpError(400, "invalid_json", "入力内容の形式が正しくありません。")
        return body


def _not_found() -> _HttpError:
    return _HttpError(404, "entry_not_found", "明細が見つかりません。削除された可能性があります。")


def _validation_error(exc: LedgerValidationError) -> _HttpError:
    return _HttpError(400, "validation_error", "入力内容を確認してください。", {"fields": exc.errors})

