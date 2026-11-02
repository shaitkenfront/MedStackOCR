"""支払先（医療機関・薬局など）の名前の表記ゆれと、OCR の読み取りミスの検出。

名前の比較は「比較キー」で行う。比較キーは次の違いを吸収した文字列:
- 全角/半角（NFKC 正規化）、空白・中黒などの区切り
- 「医療法人社団」「株式会社」などの法人格
- OCR で取り違えやすい文字（ロ/口、カ/力、ー/一 など）
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

SIMILARITY_THRESHOLD = 0.8
MIN_CONTAINMENT_LENGTH = 4

_CORPORATE_WORDS = (
    "社会医療法人社団",
    "社会医療法人財団",
    "社会医療法人",
    "医療法人社団",
    "医療法人財団",
    "医療法人",
    "一般社団法人",
    "一般財団法人",
    "公益社団法人",
    "公益財団法人",
    "社会福祉法人",
    "独立行政法人",
    "地方独立行政法人",
    "株式会社",
    "有限会社",
    "合同会社",
    "(株)",
    "(有)",
    "(医)",
    "(医社)",
    "(財)",
    "(社)",
)

# OCR で取り違えやすい文字を同じ文字に寄せる（比較キー専用。表示用の名前は変えない）
_CONFUSABLES = str.maketrans(
    {
        "口": "ロ",
        "力": "カ",
        "工": "エ",
        "一": "ー",
        "―": "ー",
        "‐": "ー",
        "-": "ー",
        "卜": "ト",
        "二": "ニ",
        "八": "ハ",
        "夕": "タ",
        "へ": "ヘ",
        "べ": "ベ",
        "ぺ": "ペ",
        "o": "0",
        "l": "1",
        "|": "1",
    }
)

_SEPARATORS_RE = re.compile(r"[\s・･.,、。\"'`]+")
_ALLOWED_CHAR_RE = re.compile(
    r"[0-9A-Za-z\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3005\u3006()&\-\u30fb .\u3000]"
)
_KATAKANA = r"\u30a1-\u30fa"
_SANDWICHED_IN_KATAKANA_RE = re.compile(rf"[{_KATAKANA}][0-9A-Za-z][{_KATAKANA}]")


def normalize_display_name(name: Any) -> str:
    """表示用の名前を整える（NFKC と空白の正規化だけ。文字そのものは変えない）。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.split())


def comparison_key(name: Any) -> str:
    text = normalize_display_name(name).lower()
    for word in _CORPORATE_WORDS:
        text = text.replace(word.lower(), "")
    text = _SEPARATORS_RE.sub("", text)
    return text.translate(_CONFUSABLES)


def are_similar(a: str, b: str) -> bool:
    key_a, key_b = comparison_key(a), comparison_key(b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    shorter, longer = sorted((key_a, key_b), key=len)
    if len(shorter) >= MIN_CONTAINMENT_LENGTH and shorter in longer:
        return True
    return SequenceMatcher(None, key_a, key_b).ratio() >= SIMILARITY_THRESHOLD


def suspicious_reason(name: Any) -> str | None:
    """OCR の読み取りミスが疑われる理由。問題なさそうなら None。"""
    display = normalize_display_name(name)
    if not display:
        return None
    if len(comparison_key(display)) <= 2:
        return "名前が短すぎます"
    odd = [ch for ch in display if not _ALLOWED_CHAR_RE.match(ch)]
    if odd:
        return "読み取りミスの可能性がある記号が含まれています"
    if _SANDWICHED_IN_KATAKANA_RE.search(display):
        return "カタカナの間に数字や英字が混ざっています"
    return None


def pair_key(a: str, b: str) -> str:
    """「別の支払先」として記録する2つの名前の組のキー（順序に依存しない）。"""
    first, second = sorted((comparison_key(a), comparison_key(b)))
    return hashlib.sha1(f"{first}\u0000{second}".encode("utf-8")).hexdigest()


def alias_key(name: str) -> str:
    """OCR 後の自動置き換えに使う、読み取った名前のキー。"""
    return hashlib.sha1(comparison_key(name).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ProviderStat:
    name: str
    count: int = 0
    total_amount_yen: int = 0
    entry_ids: list[str] = field(default_factory=list)
    suspicious_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "total_amount_yen": self.total_amount_yen,
            "suspicious_reason": self.suspicious_reason,
        }


@dataclass(slots=True)
class VariantGroup:
    group_id: str
    names: list[ProviderStat]
    suggested_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "suggested_name": self.suggested_name,
            "names": [stat.to_dict() for stat in self.names],
        }


def provider_stats(entries: Iterable[Any]) -> list[ProviderStat]:
    """明細を支払先の名前ごとに集計する（件数の多い順）。"""
    stats: dict[str, ProviderStat] = {}
    for entry in entries:
        name = getattr(entry, "provider_name", None)
        if not name:
            continue
        stat = stats.setdefault(name, ProviderStat(name=name, suspicious_reason=suspicious_reason(name)))
        stat.count += 1
        stat.total_amount_yen += getattr(entry, "amount_yen", None) or 0
        stat.entry_ids.append(getattr(entry, "entry_id", ""))
    return sorted(stats.values(), key=lambda stat: (-stat.count, stat.name))


def find_variant_groups(stats: list[ProviderStat], distinct_pair_keys: Iterable[str] = ()) -> list[VariantGroup]:
    """似た名前を組にまとめる。利用者が「別の支払先」とした組み合わせはつながない。"""
    dismissed = set(distinct_pair_keys)
    parent = list(range(len(stats)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(stats)):
        for j in range(i + 1, len(stats)):
            if pair_key(stats[i].name, stats[j].name) in dismissed:
                continue
            if are_similar(stats[i].name, stats[j].name):
                parent[find(i)] = find(j)

    clusters: dict[int, list[ProviderStat]] = {}
    for index, stat in enumerate(stats):
        clusters.setdefault(find(index), []).append(stat)

    groups: list[VariantGroup] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # 件数が多く、読み取りミスの疑いがなく、より詳しい（長い）名前を正しい候補にする
        ordered = sorted(
            members,
            key=lambda stat: (stat.suspicious_reason is not None, -stat.count, -len(stat.name), stat.name),
        )
        group_id = hashlib.sha1("\u0000".join(sorted(stat.name for stat in members)).encode("utf-8")).hexdigest()[:16]
        groups.append(VariantGroup(group_id=group_id, names=ordered, suggested_name=ordered[0].name))
    groups.sort(key=lambda group: -sum(stat.count for stat in group.names))
    return groups
