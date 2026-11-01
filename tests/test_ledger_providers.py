from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, TemplateMatch
from inbox.conversation_service import ConversationService
from inbox.repository import InboxRepository
from ledger.checks import CODE_PROVIDER_SUSPICIOUS, CODE_PROVIDER_VARIANT
from ledger.providers import (
    ProviderStat,
    are_similar,
    comparison_key,
    find_variant_groups,
    pair_key,
    suspicious_reason,
)
from ledger.service import LedgerService
from ledger.validation import LedgerValidationError


class ProviderNameTest(unittest.TestCase):
    def test_comparison_key_absorbs_width_spaces_and_corporate_words(self) -> None:
        self.assertEqual(comparison_key("医療法人社団 健康会　山田内科"), comparison_key("健康会山田内科"))
        self.assertEqual(comparison_key("ｻｸﾗ薬局"), comparison_key("サクラ薬局"))
        # OCR で取り違えやすい文字（口/ロ、力/カ）
        self.assertEqual(comparison_key("口ーソン薬局"), comparison_key("ローソン薬局"))
        self.assertEqual(comparison_key("力ワイ病院"), comparison_key("カワイ病院"))

    def test_similarity(self) -> None:
        self.assertTrue(are_similar("さくら薬局 駅前店", "さくら薬局駅前庖"))
        self.assertTrue(are_similar("山田内科クリニック", "山田内科ク1ニック"))
        self.assertTrue(are_similar("さくら薬局", "さくら薬局 駅前店"))  # 片方を含む
        # 同じチェーンの別店舗は別の支払先として扱う
        self.assertFalse(are_similar("さくら薬局 駅前店", "さくら薬局 本町店"))
        self.assertFalse(are_similar("山田内科", "田中歯科"))

    def test_suspicious_reason(self) -> None:
        self.assertIsNotNone(suspicious_reason("山田内科ク1ニック"))
        self.assertIsNotNone(suspicious_reason("山田内科#"))
        self.assertIsNotNone(suspicious_reason("ab"))
        for name in ("山﨑医院", "第2病院", "A&Bクリニック", "さくら薬局 駅前店", "ウエルシア薬局"):
            self.assertIsNone(suspicious_reason(name), name)

    def test_groups_prefer_frequent_clean_name_and_respect_dismissals(self) -> None:
        stats = [
            ProviderStat("さくら薬局駅前店", count=3),
            ProviderStat("さくら薬局 駅前店", count=12),
            ProviderStat("さくら薬局 駅前庖", count=1),
            ProviderStat("山田内科", count=5),
        ]
        groups = find_variant_groups(stats)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].suggested_name, "さくら薬局 駅前店")
        self.assertEqual({stat.name for stat in groups[0].names}, {"さくら薬局駅前店", "さくら薬局 駅前店", "さくら薬局 駅前庖"})

        dismissed = [pair_key(a, b) for a, b in [
            ("さくら薬局駅前店", "さくら薬局 駅前店"),
            ("さくら薬局駅前店", "さくら薬局 駅前庖"),
            ("さくら薬局 駅前店", "さくら薬局 駅前庖"),
        ]]
        self.assertEqual(find_variant_groups(stats, dismissed), [])

    def test_suspicious_name_is_not_suggested(self) -> None:
        stats = [ProviderStat("山田内科ク1ニック", count=5, suspicious_reason="x"), ProviderStat("山田内科クリニック", count=1)]
        self.assertEqual(find_variant_groups(stats)[0].suggested_name, "山田内科クリニック")


class ProviderServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InboxRepository(str(Path(self._tmp.name) / "linebot.db"))
        self.service = LedgerService(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, provider: str, date: str = "2026-03-10", amount: int = 1000, receipt_id: str | None = None) -> str:
        if receipt_id is None:
            return self.service.create_manual_entry(
                "U1",
                {"service_date": date, "provider_name": provider, "family_member_name": "山田 太郎", "amount_yen": amount},
            ).entry_id
        self.repo.upsert_aggregate_entry(
            receipt_id=receipt_id,
            line_user_id="U1",
            fields={
                FieldName.PAYMENT_DATE: date,
                FieldName.PAYMENT_AMOUNT: amount,
                FieldName.PAYER_FACILITY_NAME: provider,
                FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
            },
            status="confirmed",
        )
        return receipt_id

    def test_overview_and_checks_report_variants(self) -> None:
        self.add("さくら薬局 駅前店", amount=1000)
        self.add("さくら薬局 駅前店", "2026-04-01", amount=2000)
        variant_id = self.add("さくら薬局駅前庖", "2026-05-01", amount=500)
        self.add("山田内科ク1ニック", "2026-06-01")

        overview = self.service.provider_overview("U1", 2026)
        self.assertEqual(overview["providers"][0], {
            "name": "さくら薬局 駅前店", "count": 2, "total_amount_yen": 3000, "suspicious_reason": None,
        })
        self.assertEqual(len(overview["groups"]), 1)

        issues = self.service.check("U1", 2026).issues
        codes = {(issue.entry_id, issue.code) for issue in issues}
        self.assertIn((variant_id, CODE_PROVIDER_VARIANT), codes)
        self.assertEqual(sum(1 for issue in issues if issue.code == CODE_PROVIDER_SUSPICIOUS), 1)

    def test_rename_updates_all_years_and_learns_alias(self) -> None:
        self.add("さくら薬局 駅前店")
        old_id = self.add("さくら薬局駅前庖", "2025-12-01", receipt_id="R1")
        updated = self.service.rename_providers("U1", ["さくら薬局駅前庖"], "さくら薬局 駅前店")
        self.assertEqual(updated, 1)
        self.assertEqual(self.service.get_entry("U1", old_id).provider_name, "さくら薬局 駅前店")
        # OCR 側の項目値にも反映される
        self.assertEqual(self.repo.get_receipt_fields("R1")[FieldName.PAYER_FACILITY_NAME], "さくら薬局 駅前店")
        self.assertEqual(self.service.provider_overview("U1", 2025)["groups"], [])

    def test_rename_validates_input(self) -> None:
        with self.assertRaises(LedgerValidationError):
            self.service.rename_providers("U1", [], "さくら薬局")
        with self.assertRaises(LedgerValidationError):
            self.service.rename_providers("U1", ["a"], "  ")

    def test_mark_distinct_hides_group(self) -> None:
        self.add("さくら薬局")
        self.add("さくら薬局 本町店", "2026-04-01")
        self.assertEqual(len(self.service.provider_overview("U1", 2026)["groups"]), 1)
        self.service.mark_providers_distinct("U1", ["さくら薬局", "さくら薬局 本町店"])
        self.assertEqual(self.service.provider_overview("U1", 2026)["groups"], [])
        codes = {issue.code for issue in self.service.check("U1", 2026).issues}
        self.assertNotIn(CODE_PROVIDER_VARIANT, codes)
        with self.assertRaises(LedgerValidationError):
            self.service.mark_providers_distinct("U1", ["さくら薬局"])

    def test_next_ocr_uses_learned_name(self) -> None:
        self.add("さくら薬局 駅前店")
        self.add("さくら薬局駅前庖", "2026-05-01")
        self.service.rename_providers("U1", ["さくら薬局駅前庖"], "さくら薬局 駅前店")

        conversation = ConversationService(self.repo)
        messages = conversation.handle_new_result("U1", "R9", _ocr_result("さくら薬局 駅前庖"))
        session = self.repo.get_active_session("U1")
        assert session is not None
        self.assertEqual(session.payload["fields"][FieldName.PAYER_FACILITY_NAME], "さくら薬局 駅前店")
        self.assertTrue(any("さくら薬局 駅前店" in str(message.get("text", "")) for message in messages))

    def test_next_ocr_keeps_unknown_name(self) -> None:
        conversation = ConversationService(self.repo)
        conversation.handle_new_result("U1", "R9", _ocr_result("はじめて薬局"))
        session = self.repo.get_active_session("U1")
        assert session is not None
        self.assertEqual(session.payload["fields"][FieldName.PAYER_FACILITY_NAME], "はじめて薬局")


def _ocr_result(facility: str) -> ExtractionResult:
    def candidate(field: str, value: Any) -> Candidate:
        return Candidate(
            field=field,
            value_raw=value,
            value_normalized=value,
            source_line_indices=[0],
            bbox=None,
            score=5.0,
            ocr_confidence=0.9,
            reasons=["test"],
            source="generic",
        )

    year = datetime.now(timezone.utc).year
    return ExtractionResult(
        document_id="doc",
        household_id=None,
        document_type=DocumentType.PHARMACY,
        template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
        fields={
            FieldName.PAYER_FACILITY_NAME: candidate(FieldName.PAYER_FACILITY_NAME, facility),
            FieldName.PAYMENT_DATE: candidate(FieldName.PAYMENT_DATE, f"{year}-03-10"),
            FieldName.PAYMENT_AMOUNT: candidate(FieldName.PAYMENT_AMOUNT, 1200),
            FieldName.FAMILY_MEMBER_NAME: candidate(FieldName.FAMILY_MEMBER_NAME, "山田 太郎"),
        },
        decision=Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.95, reasons=[]),
        audit=AuditInfo(engine="test", engine_version="1", pipeline_version="0.1.0"),
    )


if __name__ == "__main__":
    unittest.main()
