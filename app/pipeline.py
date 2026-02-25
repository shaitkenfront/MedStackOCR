from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.logger import AuditLogger
from classify.document_classifier import DocumentClassifier
from core.enums import DecisionStatus, FieldName
from core.models import Candidate, Decision, ExtractionResult, TemplateMatch
from extractors.amount_extractor import AmountExtractor
from extractors.date_extractor import DateExtractor
from extractors.facility_extractor import FacilityExtractor
from extractors.family_name_extractor import FamilyNameExtractor
from io_utils.image_loader import get_image_size
from ocr.factory import create_ocr_adapter
from ocr.normalizer import OCRNormalizer
from resolver.decision_resolver import resolver_from_config
from templates.matcher import TemplateMatcher
from templates.store import TemplateStore


class ReceiptExtractionPipeline:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        template_conf = config.get("templates", {})
        self.template_store = TemplateStore(template_conf.get("store_path", "data/templates"))
        self.template_matcher = TemplateMatcher(
            store=self.template_store,
            match_threshold=float(template_conf.get("household_match_threshold", 0.65)),
        )
        self.classifier = DocumentClassifier()
        self.facility_extractor = FacilityExtractor()
        self.date_extractor = DateExtractor()
        documentai_tuning = (
            config.get("ocr", {})
            .get("engines", {})
            .get("documentai", {})
            .get("amount_tuning", {})
        )
        self.amount_extractor = AmountExtractor(documentai_tuning=documentai_tuning)
        self.family_name_extractor = FamilyNameExtractor(config.get("family_registry"))
        self.resolver = resolver_from_config(config)
        self.normalizer = OCRNormalizer()
        self.audit_logger = AuditLogger()

    def process(
        self,
        image_path: str,
        household_id: str | None,
        ocr_engine: str,
        family_registry_override: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        adapter = create_ocr_adapter(ocr_engine, self.config)
        raw = adapter.run(image_path)
        image_size = get_image_size(image_path)
        lines = self.normalizer.normalize(raw=raw, image_size=image_size)
        family_name_extractor = self.family_name_extractor
        if family_registry_override is not None:
            family_name_extractor = FamilyNameExtractor(family_registry_override)

        document_type, _, classifier_reasons, ocr_quality = self.classifier.classify(lines)
        template_match = TemplateMatch(matched=False, template_family_id=None, score=0.0, reasons=["template_not_checked"])
        matched_template: dict[str, Any] | None = None
        if household_id:
            template_match, matched_template = self.template_matcher.match(
                household_id=household_id,
                document_type=document_type.value,
                lines=lines,
            )

        candidate_pool: dict[str, list[Candidate]] = {
            FieldName.PAYER_FACILITY_NAME: [],
            FieldName.PRESCRIBING_FACILITY_NAME: [],
            FieldName.PAYMENT_DATE: [],
            FieldName.PAYMENT_AMOUNT: [],
            FieldName.FAMILY_MEMBER_NAME: [],
        }
        self._merge_candidate_pool(candidate_pool, self.facility_extractor.extract(document_type, lines))
        candidate_pool[FieldName.PAYMENT_DATE].extend(self.date_extractor.extract(lines))
        candidate_pool[FieldName.PAYMENT_AMOUNT].extend(
            self.amount_extractor.extract(lines, ocr_engine=raw.engine)
        )
        candidate_pool[FieldName.FAMILY_MEMBER_NAME].extend(family_name_extractor.extract(lines))

        if matched_template is not None:
            template_candidates = self.template_matcher.apply_template(matched_template, lines)
            self._merge_candidate_pool(candidate_pool, template_candidates)

        for key in candidate_pool:
            candidate_pool[key] = sorted(candidate_pool[key], key=lambda c: c.score, reverse=True)

        selected_fields, decision = self.resolver.resolve(
            candidate_pool=candidate_pool,
            template_match=template_match,
            ocr_quality=ocr_quality,
        )
        decision = self._apply_family_policy(selected_fields, decision)

        audit = self.audit_logger.create(
            engine=raw.engine,
            engine_version=raw.engine_version,
            classifier_reasons=classifier_reasons,
            notes=[],
        )
        if template_match.matched:
            audit.notes.append(f"template_applied:{template_match.template_family_id}")
        if not lines:
            audit.notes.append("ocr_lines_empty")
        family_member = selected_fields.get(FieldName.FAMILY_MEMBER_NAME)
        if family_member is None:
            audit.notes.append("family_member_not_detected")
        elif family_member.source == "family_registry":
            audit.notes.append("family_member_registry_matched")
        elif family_member.source == "family_registry_same_surname":
            audit.notes.append("family_member_unregistered_same_surname")
        elif family_member.source == "family_registry_unknown_surname":
            audit.notes.append("family_member_unregistered_different_surname")

        document_id = self._build_document_id(image_path)
        result = ExtractionResult(
            document_id=document_id,
            household_id=household_id,
            document_type=document_type,
            template_match=template_match,
            fields=selected_fields,
            decision=decision,
            audit=audit,
            candidate_pool={key: values[:5] for key, values in candidate_pool.items()},
            ocr_lines=lines,
        )
        return result

    @staticmethod
    def _merge_candidate_pool(
        target: dict[str, list[Candidate]],
        source: dict[str, list[Candidate]],
    ) -> None:
        for field_name, candidates in source.items():
            if field_name not in target:
                target[field_name] = []
            target[field_name].extend(candidates)

    @staticmethod
    def _apply_family_policy(
        selected_fields: dict[str, Candidate | None],
        decision: Decision,
    ) -> Decision:
        family_member = selected_fields.get(FieldName.FAMILY_MEMBER_NAME)
        if family_member is None:
            return decision

        reasons = list(decision.reasons)
        if family_member.source == "family_registry_unknown_surname":
            if "family_name_not_in_registry_different_surname" not in reasons:
                reasons.append("family_name_not_in_registry_different_surname")
            return Decision(status=DecisionStatus.REJECTED, confidence=decision.confidence, reasons=reasons)

        if family_member.source == "family_registry_same_surname" and decision.status != DecisionStatus.REJECTED:
            if "family_name_not_in_registry_same_surname" not in reasons:
                reasons.append("family_name_not_in_registry_same_surname")
            return Decision(status=DecisionStatus.REVIEW_REQUIRED, confidence=decision.confidence, reasons=reasons)

        return decision

    @staticmethod
    def _build_document_id(image_path: str) -> str:
        stem = Path(image_path).stem
        now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{now}_{stem}"
