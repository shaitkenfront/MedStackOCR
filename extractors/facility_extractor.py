"""医療機関・薬局の名前（支払先と処方元）の候補を OCR の行から探す。

「名前らしさ」は次の手がかりを組み合わせて点数にする。手がかりが1つもない行は候補にしない。
- 施設の種類を表す語（病院・クリニック・歯科・薬局・ドラッグ…）や「〇〇店」、法人格
- 利用者がこれまでに確定した支払先の名前との一致（同じ医療機関に繰り返し通うため強い手がかりになる）
- 文字の大きさ（行の高さが周りより大きい）、紙面の上部、住所・電話番号の欄のすぐ上という配置

住所・電話番号・金額・日付・あいさつ・明細項目・患者名などの行は、名前ではないものとして除外する。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from statistics import median
from typing import Iterable

from core.enums import DocumentType, FieldName
from core.models import Candidate, OCRLine
from extractors.common import contains_any, count_digits, normalize_spaces
from ledger.providers import comparison_key

PHARMACY_KEYWORDS = ("薬局", "調剤", "ファーマシー", "保険薬局", "薬店", "ドラッグ", "薬房")
CLINIC_KEYWORDS = (
    "病院",
    "医院",
    "クリニック",
    "診療所",
    "歯科",
    "眼科",
    "皮膚科",
    "耳鼻咽喉科",
    "耳鼻科",
    "小児科",
    "内科",
    "外科",
    "婦人科",
    "泌尿器科",
    "精神科",
    "心療内科",
    "医療センター",
    "メディカルセンター",
    "健診センター",
    "検診センター",
    "ホスピタル",
    "デンタル",
    "接骨院",
    "整骨院",
    "鍼灸院",
    "治療院",
    "訪問看護",
)
FACILITY_KEYWORDS = PHARMACY_KEYWORDS + CLINIC_KEYWORDS
# 診療科の一覧の行（「内科・小児科」など）を見分けるための診療科名
DEPARTMENT_WORDS = CLINIC_KEYWORDS + (
    "形成外科",
    "整形外科",
    "脳神経外科",
    "心臓血管外科",
    "呼吸器外科",
    "消化器外科",
    "循環器内科",
    "消化器内科",
    "呼吸器内科",
    "神経内科",
    "脳神経内科",
    "糖尿病内科",
    "腎臓内科",
    "リウマチ科",
    "アレルギー科",
    "麻酔科",
    "放射線科",
    "リハビリテーション科",
    "産科",
    "矯正歯科",
    "小児歯科",
    "口腔外科",
    "歯科口腔外科",
)
CORPORATE_WORDS = (
    "医療法人",
    "社会医療法人",
    "医療生活協同組合",
    "株式会社",
    "有限会社",
    "合同会社",
    "(株)",
    "(有)",
    "㈱",
    "㈲",
    "社団法人",
    "財団法人",
    "社会福祉法人",
    "独立行政法人",
)
PRESCRIBING_CONTEXT = ("処方箋", "処方せん", "保険医療機関", "交付", "医師", "先生")
CONTACT_ANCHORS = ("〒", "TEL", "Tel", "tel", "℡", "電話", "FAX", "Fax")
# 発行元の欄の目印（近くにあれば少し加点する。住所・電話番号ほど強い手がかりではない）
ISSUER_ANCHORS = CONTACT_ANCHORS + ("領収書", "発行")

NON_NAME_HINTS = (
    # 帳票の見出し・項目名
    "領収日",
    "発行日",
    "調剤日",
    "受診日",
    "診療日",
    "請求額",
    "請求金額",
    "請求書",
    "合計",
    "お支払",
    "税込",
    "税率",
    "点数",
    "負担割合",
    "保険種類",
    "保険適用",
    "氏名",
    "患者",
    "生年月日",
    "保険者番号",
    "記号番号",
    "明細",
    "領収",
    "請求",
    "報酬",
    "診療費",
    "診療科",
    "受付",
    "番号",
    "レジ",
    "伝票",
    "カルテ",
    "担当",
    "院長",
    "理事長",
    "薬剤師",
    "管理者",
    # 明細の項目
    "初診",
    "再診",
    "薬剤料",
    "調剤料",
    "技術料",
    "基本料",
    "管理料",
    "加算",
    "検査",
    "処置",
    "投薬",
    "注射",
    "画像診断",
    "手術",
    "麻酔",
    "食事療養",
    "医学管理",
    "専門療法",
    "放射線治療",
    "病理診断",
    "歯冠修復",
    "欠損補",
    "補綴",
    "歯科矯正",
    "負担",
    "内訳",
    "保険外",
    "自費",
    "一部負担",
    "小計",
    "消費税",
    "預り",
    "お預",
    "釣",
    "現金",
    "クレジット",
    "ポイント",
    "収入印紙",
    "上記",
    "正に",
    # あいさつ・案内
    "ありがとう",
    "お大事",
    "またの",
    "ご来店",
    "ご来院",
    "お願い",
    "ください",
    "下さい",
    "ませ",
    "お薬手帳",
    "後発医薬品",
    "ジェネリック",
    "再発行",
    "保管",
    "お知らせ",
    "ご案内",
    "について",
)
NON_NAME_EXACT = {
    "調剤",
    "明細",
    "領収",
    "合計",
    "内訳",
    # 名前ではなく、薬局の種類や認定を表す表示
    "保険薬局",
    "健康サポート薬局",
    "かかりつけ薬局",
    "調剤薬局",
    "保険調剤",
}

RE_NAME_PREFIX = re.compile(
    r"^(処方箋交付医療機関|保険医療機関|医療機関名|医療機関|病院名|医院名|薬局名|調剤薬局名|"
    r"発行元|請求元|発行者|店舗名|店名|施設名)\s*[:：]?\s*"
)
# 「クリニック名:」「医療機関名称：」のような「〇〇名:」形式のラベル
RE_GENERIC_NAME_LABEL = re.compile(r"^[^\s:：]{1,10}(名|名称)\s*[:：]\s*")
# 処方元の行に続く医師名（例: 「岡山西眼科 吉武恭子先生」）
RE_DOCTOR_SUFFIX = re.compile(r"\s+[^\s]{1,10}(先生|医師)$")
RE_CONTACT_SPLIT = re.compile(r"(TEL|Tel|tel|℡|電話|FAX|Fax|〒)")
RE_ADDRESS = re.compile(
    r"(東京都|北海道|(京都|大阪)府|.{2,3}県).{0,8}?[市区町村郡]"
    r"|\d+\s*丁目|\d+\s*番地|\d+\s*[-－ー‐]\s*\d+\s*[-－ー‐]\s*\d+"
)
RE_PHONE = re.compile(r"\d{2,4}\s*[-－ー‐(（)）]\s*\d{2,4}\s*[-－ー‐]\s*\d{3,4}")
RE_URL = re.compile(r"(https?://|www\.|\.co\.jp|\.or\.jp|\.jp\b|\.com\b|@)", re.IGNORECASE)
RE_TIME = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
RE_MONEY = re.compile(r"[¥￥]\s*\d|\d[\d,]*\s*円")
RE_DATE = re.compile(r"\d{2,4}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}|(令和|平成|R|H)\s*\d{1,2}\s*年")
RE_STORE_SUFFIX = re.compile(r"[^\d\s]{2,}\s*(本店|支店|店|営業所)$")
RE_HIRAGANA_RUN = re.compile(r"[\u3041-\u309f]{2,}")
MAX_NAME_TAIL_LENGTH = 10

# 2行にまたがる名前（例: 「東京医科大学」＋「八王子医療センター」）の1行目によく現れる語
NAME_PREFIX_HINTS = (
    "大学",
    "学園",
    "学院",
    "財団",
    "連合会",
    "組合",
    "協会",
    "赤十字",
    "済生会",
    "共済",
    "厚生",
    "労災",
    "記念",
    "国立",
    "公立",
    "都立",
    "道立",
    "府立",
    "県立",
    "市立",
    "区立",
    "町立",
    "村立",
    "附属",
    "付属",
)
JOINED_NAME_BONUS = 0.4
MAX_NAME_PREFIX_LENGTH = 20

KNOWN_EXACT_BONUS = 6.0
KNOWN_SIMILAR_BONUS = 4.0
KNOWN_PARTIAL_BONUS = 2.0
# 8文字の名前で1文字の読み違い（例: 駅前店→駅前庖）が 0.875 になる。別店舗（駅前店/本町店）は 0.75 程度
KNOWN_REPLACE_RATIO = 0.85


class FacilityExtractor:
    def extract(
        self,
        document_type: DocumentType,
        lines: list[OCRLine],
        known_names: Iterable[str] | None = None,
    ) -> dict[str, list[Candidate]]:
        candidates: dict[str, list[Candidate]] = {
            FieldName.PAYER_FACILITY_NAME: [],
            FieldName.PRESCRIBING_FACILITY_NAME: [],
        }
        if not lines:
            return candidates

        context = _PageContext(lines, known_names or ())
        for line in lines:
            cleaned = self._clean_name(line.text)
            if not self._looks_like_name(cleaned):
                continue
            payer = self._score_payer(document_type, line, cleaned, context)
            if payer is not None:
                candidates[FieldName.PAYER_FACILITY_NAME].append(payer)
                joined = self._joined_with_line_above(document_type, line, cleaned, context)
                if joined is not None:
                    candidates[FieldName.PAYER_FACILITY_NAME].append(joined)
            if document_type == DocumentType.PHARMACY:
                prescribing = self._score_pharmacy_prescribing(line, cleaned, context)
                if prescribing is not None:
                    candidates[FieldName.PRESCRIBING_FACILITY_NAME].append(prescribing)

        for key in candidates:
            candidates[key].sort(key=lambda c: c.score, reverse=True)
        return candidates

    # --- 支払先 ---

    def _score_payer(
        self,
        document_type: DocumentType,
        line: OCRLine,
        cleaned: str,
        context: "_PageContext",
    ) -> Candidate | None:
        score = 0.5
        reasons: list[str] = []
        evidence = False
        value = cleaned

        has_pharmacy = contains_any(cleaned, PHARMACY_KEYWORDS)
        has_clinic = contains_any(cleaned, CLINIC_KEYWORDS)
        if has_pharmacy or has_clinic:
            evidence = True
            if _keyword_ends_name(cleaned):
                score += 3.0
                reasons.append("facility_keyword_at_name_end")
            else:
                # 「薬局からのお知らせ」のように、施設の語の後ろに文章が続く行は名前らしさが弱い
                score += 1.0
                reasons.append("facility_keyword_in_sentence")
            if (document_type == DocumentType.PHARMACY and has_pharmacy) or (
                document_type == DocumentType.CLINIC_OR_HOSPITAL and has_clinic
            ):
                score += 0.5
                reasons.append("keyword_matches_document_type")
        if RE_STORE_SUFFIX.search(cleaned):
            score += 1.2
            evidence = True
            reasons.append("store_suffix")
        # 法人格は名前から外していることがあるので、元の行で判定する
        if contains_any(line.text, CORPORATE_WORDS):
            score += 0.6
            evidence = True
            reasons.append("contains_corporate_word")
            if context.is_corporate_only(line, cleaned):
                score -= 1.5
                reasons.append("corporate_only_line_penalty")
        if context.follows_corporate_line(line):
            score += 1.0
            reasons.append("follows_corporate_line")

        known = context.match_known(cleaned)
        if known is not None:
            kind, name = known
            evidence = True
            if kind == "exact":
                score += KNOWN_EXACT_BONUS
                value = name
            elif kind == "similar":
                score += KNOWN_SIMILAR_BONUS
                value = name
            else:
                score += KNOWN_PARTIAL_BONUS
            reasons.append(f"known_provider_{kind}")

        height_ratio = context.height_ratio(line)
        if height_ratio >= 1.35:
            score += 1.5
            evidence = True
            reasons.append("large_text")
        elif height_ratio >= 1.15:
            score += 0.7
            reasons.append("slightly_large_text")
        elif height_ratio < 0.8:
            score -= 0.5
            reasons.append("small_text_penalty")

        if line.center()[1] <= 0.25:
            score += 0.8
            reasons.append("top_region_bonus")
        if context.is_above_contact_block(line):
            score += 1.5
            evidence = True
            reasons.append("above_contact_block")
        elif context.is_near_contact(line):
            score += 0.6
            reasons.append("near_contact_anchor")

        if contains_any(line.text, PRESCRIBING_CONTEXT):
            score -= 4.0 if document_type == DocumentType.PHARMACY else 2.0
            reasons.append("prescribing_context_penalty")
        elif document_type == DocumentType.PHARMACY and context.is_near_prescribing(line):
            score -= 3.0
            reasons.append("near_prescribing_context_penalty")
        if document_type == DocumentType.PHARMACY and has_clinic and not has_pharmacy:
            # 薬局の領収書に載る病院名は、たいてい処方箋を出した医療機関。
            # 家族の通院先は確定済みの支払先にも入っているので、一致していても減点する
            score -= 2.5
            reasons.append("clinic_name_on_pharmacy_receipt_penalty")

        if not evidence or score < 1.0:
            return None
        return Candidate(
            field=FieldName.PAYER_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=value,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=round(score, 3),
            ocr_confidence=line.confidence,
            reasons=reasons,
        )

    def _joined_with_line_above(
        self,
        document_type: DocumentType,
        line: OCRLine,
        cleaned: str,
        context: "_PageContext",
    ) -> Candidate | None:
        """名前が2行にまたがっていそうなら、上の行とつないだ候補を作る（2行目だけが残るのを防ぐ）。"""
        above = context.name_prefix_line(line)
        if above is None:
            return None
        prefix = self._clean_name(above.text)
        joined_text = f"{prefix} {cleaned}"
        joined = self._score_payer(document_type, line, joined_text, context)
        if joined is None:
            return None
        joined.score = round(joined.score + JOINED_NAME_BONUS, 3)
        joined.reasons.append("joined_with_line_above")
        joined.value_raw = f"{above.text} {line.text}"
        joined.source_line_indices = [above.line_index, line.line_index]
        joined.bbox = (
            min(above.bbox[0], line.bbox[0]),
            min(above.bbox[1], line.bbox[1]),
            max(above.bbox[2], line.bbox[2]),
            max(above.bbox[3], line.bbox[3]),
        )
        return joined

    # --- 処方元（薬局の領収書に載る、処方箋を出した医療機関） ---

    def _score_pharmacy_prescribing(
        self,
        line: OCRLine,
        cleaned: str,
        context: "_PageContext",
    ) -> Candidate | None:
        has_clinic = contains_any(cleaned, CLINIC_KEYWORDS)
        near_anchor = contains_any(line.text, PRESCRIBING_CONTEXT) or context.is_near_prescribing(line)
        if not has_clinic and not (near_anchor and contains_any(cleaned, CORPORATE_WORDS)):
            return None
        score = 0.8
        reasons: list[str] = []
        if near_anchor:
            score += 3.0
            reasons.append("near_prescribing_anchor")
        if has_clinic:
            score += 2.0
            reasons.append("contains_clinic_keyword")
        if contains_any(cleaned, PHARMACY_KEYWORDS):
            score -= 3.0
            reasons.append("contains_pharmacy_keyword_penalty")
        if 0.18 <= line.center()[1] <= 0.6:
            score += 0.6
            reasons.append("middle_region_bonus")
        if score < 1.0:
            return None
        return Candidate(
            field=FieldName.PRESCRIBING_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=cleaned,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=round(score, 3),
            ocr_confidence=line.confidence,
            reasons=reasons,
        )

    # --- 行の整形と除外 ---

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        t = normalize_spaces(text)
        if not t:
            return False
        compact = re.sub(r"\s+", "", t)
        if len(compact) < 3 or len(t) > 64:
            return False
        upper_t = t.upper()
        if t.startswith("〒") or upper_t.startswith("TEL") or "FAX" in upper_t:
            return False
        if any(key in compact for key in NON_NAME_HINTS):
            return False
        if compact in NON_NAME_EXACT:
            return False
        if compact.endswith(("様", "殿")):
            return False
        if _is_department_list(compact):
            return False
        if (":" in t or "：" in t) and not contains_any(t, FACILITY_KEYWORDS):
            return False
        for pattern in (RE_ADDRESS, RE_PHONE, RE_URL, RE_TIME, RE_MONEY, RE_DATE):
            if pattern.search(t):
                return False
        digits = count_digits(t)
        if digits and digits / max(1, len(compact)) > 0.35:
            return False
        symbols = sum(1 for ch in compact if not (ch.isalnum() or ch in "()（）&・ー-"))
        if symbols / max(1, len(compact)) > 0.3:
            return False
        return True

    @staticmethod
    def _clean_name(text: str) -> str:
        cleaned = normalize_spaces(text)
        # 「〇〇薬局 TEL 03-…」のように同じ行に続く連絡先を落とす
        head = RE_CONTACT_SPLIT.split(cleaned, maxsplit=1)[0].strip()
        if len(re.sub(r"\s+", "", head)) >= 2:
            cleaned = head
        cleaned = RE_NAME_PREFIX.sub("", cleaned)
        cleaned = RE_GENERIC_NAME_LABEL.sub("", cleaned)
        cleaned = RE_DOCTOR_SUFFIX.sub("", cleaned)
        cleaned = cleaned.strip(" :：")
        # 「医療法人社団〇〇 きれいわ歯科」のように空白で区切られた法人格の部分は外し、施設名だけにする
        head, _, rest = cleaned.partition(" ")
        if (
            rest
            and contains_any(head, CORPORATE_WORDS)
            and not contains_any(head, FACILITY_KEYWORDS)
            and contains_any(rest, FACILITY_KEYWORDS)
        ):
            cleaned = rest.strip()
        # 「保険薬局 さくら薬局」のような表示は、名前の部分だけにする
        if cleaned.startswith("保険薬局"):
            rest = cleaned[len("保険薬局"):].strip(" :：")
            if contains_any(rest, PHARMACY_KEYWORDS):
                cleaned = rest
        return cleaned


class _PageContext:
    """ページ全体から求める手がかり（行の高さの中央値、連絡先の行、処方の行、既知の支払先）。"""

    def __init__(self, lines: list[OCRLine], known_names: Iterable[str]) -> None:
        self.lines = lines
        heights = [
            line.bbox[3] - line.bbox[1]
            for line in lines
            if line.bbox[3] > line.bbox[1] and len(line.text.strip()) >= 2
        ]
        self.median_height = median(heights) if heights else 0.0
        self.contact_lines = [line for line in lines if _is_contact_line(line.text)]
        self.issuer_lines = [line for line in lines if contains_any(line.text, ISSUER_ANCHORS) or _is_contact_line(line.text)]
        self.prescribing_lines = [line for line in lines if contains_any(line.text, PRESCRIBING_CONTEXT)]
        self.known: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in known_names:
            text = normalize_spaces(str(name or ""))
            key = comparison_key(text)
            if len(key) >= 3 and key not in seen:
                seen.add(key)
                self.known.append((key, text))

    def height_ratio(self, line: OCRLine) -> float:
        if self.median_height <= 0:
            return 1.0
        return (line.bbox[3] - line.bbox[1]) / self.median_height

    def is_above_contact_block(self, line: OCRLine) -> bool:
        """住所・電話番号の行が、この行のすぐ下（横位置も重なる）にあるか。発行元の欄によくある配置。"""
        _, cy = line.center()
        for anchor in self.contact_lines:
            if anchor.line_index == line.line_index:
                continue
            _, ay = anchor.center()
            if 0 < ay - cy <= 0.08 and _horizontal_overlap(line, anchor):
                return True
        return False

    def is_near_contact(self, line: OCRLine) -> bool:
        return self._near_any(line, self.issuer_lines, 0.12)

    def is_near_prescribing(self, line: OCRLine) -> bool:
        return self._near_any(line, self.prescribing_lines, 0.12)

    def is_corporate_only(self, line: OCRLine, cleaned: str) -> bool:
        """「医療法人社団〇〇会」だけの行で、すぐ下に施設名の行があるか。"""
        if contains_any(cleaned, FACILITY_KEYWORDS):
            return False
        below = self._line_below(line)
        return below is not None and contains_any(below.text, FACILITY_KEYWORDS)

    def name_prefix_line(self, line: OCRLine) -> OCRLine | None:
        """名前の1行目らしい行が、すぐ上にあれば返す。

        1行目は施設の語を含まず（含むならそれ自体が名前）、文字の大きさがほぼ同じで、
        「大学」「赤十字」「県立」などの語を含むか、2行とも大きな文字で印字されている。
        「医療法人社団〇〇会」のような法人格だけの行はつながない（明細には施設名だけの方が見やすいため）。
        """
        above = self._line_above(line)
        if above is None:
            return None
        text = FacilityExtractor._clean_name(above.text)
        compact = re.sub(r"\s+", "", text)
        if not compact or len(compact) > MAX_NAME_PREFIX_LENGTH:
            return None
        if not FacilityExtractor._looks_like_name(text):
            return None
        if contains_any(text, FACILITY_KEYWORDS) or contains_any(text, CORPORATE_WORDS):
            return None
        line_height = line.bbox[3] - line.bbox[1]
        above_height = above.bbox[3] - above.bbox[1]
        if line_height <= 0 or not (0.7 <= above_height / line_height <= 1.4):
            return None
        has_hint = contains_any(text, NAME_PREFIX_HINTS)
        both_large = self.height_ratio(line) >= 1.15 and self.height_ratio(above) >= 1.15
        if not has_hint and not (both_large and not RE_HIRAGANA_RUN.search(text)):
            return None
        return above

    def follows_corporate_line(self, line: OCRLine) -> bool:
        above = self._line_above(line)
        return (
            above is not None
            and contains_any(above.text, CORPORATE_WORDS)
            and not contains_any(above.text, FACILITY_KEYWORDS)
        )

    def match_known(self, cleaned: str) -> tuple[str, str] | None:
        """確定済みの支払先との一致を返す。

        - exact: 比較キーが一致（名前を確定済みの表記に置き換える）
        - similar: 行が確定済みの名前を含む、またはほぼ同じ（名前を置き換える）
        - partial: 似ているが別の店舗などの可能性がある（加点のみで、名前は置き換えない）
        """
        key = comparison_key(cleaned)
        if len(key) < 3 or not self.known:
            return None
        best: tuple[str, str] | None = None
        best_ratio = 0.0
        for known_key, name in self.known:
            if key == known_key:
                return ("exact", name)
            if known_key in key:
                kind, ratio = "similar", 0.99
            elif key in known_key and len(key) >= 4:
                kind, ratio = "partial", 0.8
            else:
                ratio = SequenceMatcher(None, key, known_key).ratio()
                if ratio >= KNOWN_REPLACE_RATIO:
                    kind = "similar"
                elif ratio >= 0.8:
                    kind = "partial"
                else:
                    continue
            if ratio > best_ratio:
                best, best_ratio = (kind, name), ratio
        return best

    def _near_any(self, line: OCRLine, anchors: list[OCRLine], vertical_tol: float) -> bool:
        _, cy = line.center()
        cx = line.center()[0]
        for anchor in anchors:
            if anchor.line_index == line.line_index:
                continue
            ax, ay = anchor.center()
            if abs(ay - cy) <= vertical_tol and abs(ax - cx) <= 0.5:
                return True
        return False

    def _line_below(self, line: OCRLine) -> OCRLine | None:
        return self._neighbor(line, direction=1)

    def _line_above(self, line: OCRLine) -> OCRLine | None:
        return self._neighbor(line, direction=-1)

    def _neighbor(self, line: OCRLine, direction: int) -> OCRLine | None:
        _, cy = line.center()
        best: OCRLine | None = None
        best_gap = 0.06
        for other in self.lines:
            if other.line_index == line.line_index or other.page != line.page:
                continue
            gap = (other.center()[1] - cy) * direction
            if 0 < gap <= best_gap and _horizontal_overlap(line, other):
                best = other
                best_gap = gap
        return best


def _is_department_list(compact: str) -> bool:
    """「内科・小児科」のように診療科の名前だけでできた行（施設の名前ではない）か。"""
    rest = compact
    for keyword in sorted(DEPARTMENT_WORDS, key=len, reverse=True):
        rest = rest.replace(keyword, "")
    rest = re.sub(r"[・･、,/／()（）]", "", rest)
    return rest != compact and len(rest) <= 1


def _keyword_ends_name(text: str) -> bool:
    """施設を表す語が名前の終わり近くにあるか（後ろが「駅前店」「本院」程度の短い語だけか）。"""
    last_end = -1
    for keyword in FACILITY_KEYWORDS:
        index = text.rfind(keyword)
        if index >= 0:
            last_end = max(last_end, index + len(keyword))
    if last_end < 0:
        return False
    tail = text[last_end:].strip()
    return len(tail) <= MAX_NAME_TAIL_LENGTH and not RE_HIRAGANA_RUN.search(tail)


def _is_contact_line(text: str) -> bool:
    if contains_any(text, CONTACT_ANCHORS):
        return True
    return bool(RE_ADDRESS.search(text) or RE_PHONE.search(text))


def _horizontal_overlap(a: OCRLine, b: OCRLine) -> bool:
    return min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]) > 0
