from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from core.enums import FieldName
from core.models import Candidate, OCRLine
from extractors.common import count_digits, normalize_spaces

NAME_LABELS = ("患者氏名", "患者名", "氏名", "受診者氏名", "受診者", "お名前")
NON_NAME_HINTS = (
    "調剤日",
    "発行日",
    "領収日",
    "受診日",
    "診療日",
    "保険",
    "負担",
    "番号",
    "請求",
    "合計",
    "領収",
    "薬局",
    "病院",
    "クリニック",
    "医院",
    "TEL",
    "FAX",
    "〒",
)
RE_LABEL_PREFIX = re.compile(r"^(?:患者氏名|患者名|氏名|受診者氏名|受診者|お名前)\s*[:：]?\s*")
RE_HONORIFIC_SUFFIX = re.compile(r"\s*(?:様|殿)\s*$")
RE_JP_NAME_CHARS = re.compile(r"^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー・\s]+$")


class FamilyRegistryError(ValueError):
    pass


@dataclass(slots=True)
class FamilyMember:
    canonical_name: str
    aliases: list[str]
    surname: str


class FamilyRegistry:
    def __init__(self, config: dict[str, Any] | None) -> None:
        conf = config or {}
        self.required = bool(conf.get("required", True))
        self.members: list[FamilyMember] = []
        self.alias_to_canonical: dict[str, str] = {}
        self.surname_keys: set[str] = set()

        members = conf.get("members", [])
        if not isinstance(members, list):
            members = []
        self._load_members(members)

        if self.required and not self.members:
            raise FamilyRegistryError(
                "family_registry.members is required. register at least one family member in config."
            )

    @staticmethod
    def normalize_name(text: str) -> str:
        cleaned = normalize_spaces(text)
        cleaned = RE_LABEL_PREFIX.sub("", cleaned)
        cleaned = RE_HONORIFIC_SUFFIX.sub("", cleaned)
        return cleaned.strip(" :：")

    @staticmethod
    def normalize_key(text: str) -> str:
        cleaned = FamilyRegistry.normalize_name(text)
        return re.sub(r"[\s　・･\.]", "", cleaned).lower()

    @staticmethod
    def extract_surname(name: str) -> str:
        cleaned = FamilyRegistry.normalize_name(name)
        if not cleaned:
            return ""
        parts = cleaned.split(" ")
        if len(parts) >= 2 and parts[0]:
            return parts[0]
        return cleaned[:2] if len(cleaned) >= 2 else cleaned

    def resolve(self, name: str) -> tuple[str, str, str, float]:
        normalized = self.normalize_name(name)
        key = self.normalize_key(normalized)
        if not key:
            return normalized, "family_registry_unknown_surname", "family_name_empty", 0.0

        canonical = self.alias_to_canonical.get(key)
        if canonical is not None:
            canonical_key = self.normalize_key(canonical)
            if key == canonical_key:
                return canonical, "family_registry", "family_name_exact_match", 6.2
            return canonical, "family_registry", "family_name_alias_match", 5.8

        fuzzy = self._resolve_fuzzy(key)
        if fuzzy is not None:
            canonical, similarity = fuzzy
            return canonical, "family_registry", f"family_name_alias_fuzzy_match:{similarity:.2f}", 5.2

        if self._has_same_surname(normalized):
            return normalized, "family_registry_same_surname", "family_name_unregistered_same_surname", 4.0
        return normalized, "family_registry_unknown_surname", "family_name_unregistered_different_surname", 4.0

    def _load_members(self, members: list[Any]) -> None:
        for member in members:
            if not isinstance(member, dict):
                continue
            canonical = normalize_spaces(str(member.get("canonical_name", ""))).strip()
            if not canonical:
                continue
            aliases = member.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            normalized_aliases = [normalize_spaces(str(alias)).strip() for alias in aliases if str(alias).strip()]
            record = FamilyMember(
                canonical_name=canonical,
                aliases=normalized_aliases,
                surname=self.extract_surname(canonical),
            )
            self.members.append(record)

            canonical_key = self.normalize_key(canonical)
            if canonical_key:
                self.alias_to_canonical[canonical_key] = canonical
            for alias in normalized_aliases:
                alias_key = self.normalize_key(alias)
                if alias_key:
                    self.alias_to_canonical[alias_key] = canonical

            surname_key = self.normalize_key(record.surname)
            if surname_key:
                self.surname_keys.add(surname_key)

    def _resolve_fuzzy(self, key: str) -> tuple[str, float] | None:
        best_similarity = 0.0
        best_canonical: str | None = None
        for alias_key, canonical in self.alias_to_canonical.items():
            similarity = SequenceMatcher(None, key, alias_key).ratio()
            if similarity > best_similarity:
                best_similarity = similarity
                best_canonical = canonical
        if best_canonical is None or best_similarity < 0.85:
            return None
        return best_canonical, best_similarity

    def _has_same_surname(self, name: str) -> bool:
        key = self.normalize_key(name)
        if not key:
            return False
        for surname_key in self.surname_keys:
            if key.startswith(surname_key):
                return True
        return False


class FamilyNameExtractor:
    def __init__(self, registry_config: dict[str, Any] | None) -> None:
        self.registry = FamilyRegistry(registry_config)

    def extract(self, lines: list[OCRLine]) -> list[Candidate]:
        candidates: list[Candidate] = []
        for line in lines:
            candidates.extend(self._extract_from_line(line))
        return sorted(candidates, key=lambda c: (c.score, c.ocr_confidence), reverse=True)

    def _extract_from_line(self, line: OCRLine) -> list[Candidate]:
        text = normalize_spaces(line.text)
        possibilities = self._build_name_possibilities(text)
        items: list[Candidate] = []
        seen: set[str] = set()

        for name in possibilities:
            cleaned = FamilyRegistry.normalize_name(name)
            key = FamilyRegistry.normalize_key(cleaned)
            if not key or key in seen:
                continue
            seen.add(key)
            if not self._looks_like_person_name(cleaned):
                continue

            value_normalized, source, reason, score = self.registry.resolve(cleaned)
            reasons = [reason]
            if any(label in text for label in NAME_LABELS):
                score += 1.0
                reasons.append("has_name_label")
            if text.endswith(("様", "殿")):
                score += 0.4
                reasons.append("has_honorific_suffix")

            items.append(
                Candidate(
                    field=FieldName.FAMILY_MEMBER_NAME,
                    value_raw=text,
                    value_normalized=value_normalized,
                    source_line_indices=[line.line_index],
                    bbox=line.bbox,
                    score=score,
                    ocr_confidence=line.confidence,
                    reasons=reasons,
                    source=source,
                )
            )
        return items

    @staticmethod
    def _build_name_possibilities(text: str) -> list[str]:
        t = normalize_spaces(text)
        candidates: list[str] = []

        if any(label in t for label in NAME_LABELS):
            stripped = RE_LABEL_PREFIX.sub("", t).strip(" :：")
            if stripped:
                candidates.append(stripped)
        if t.endswith(("様", "殿")):
            stripped = RE_HONORIFIC_SUFFIX.sub("", t).strip()
            if stripped:
                candidates.append(stripped)
        candidates.append(t)
        return candidates

    @staticmethod
    def _looks_like_person_name(text: str) -> bool:
        t = normalize_spaces(text)
        if not t:
            return False
        if any(hint in t for hint in NON_NAME_HINTS):
            return False
        if count_digits(t) > 0:
            return False
        compact = re.sub(r"\s+", "", t)
        if len(compact) < 2 or len(compact) > 24:
            return False
        if not RE_JP_NAME_CHARS.match(t):
            return False
        return True
