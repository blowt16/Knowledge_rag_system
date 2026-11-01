"""引用支持度校验（字面规则，不调模型）。

判定只看「这句话的关键词/数字是否出现在被引片段里」，不做语义蕴含。
第一版阈值保守：内容单元少于 3 个不判；只提示，不阻断回答。

⚠️ 归一化必须去掉**所有空白**：PDF 原文会在词之间硬换行
（“专\\n科起点本科学习”），不去空白会把同一句话判成不支持。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from app.graph.nodes.generate import is_conclusion_sentence
from app.graph.state import Chunk
from app.schemas.chat import UnsupportedClaim, UnsupportedMarker

#: 阈值初版写死，后续用标注样本校准；校准前只作为提示，不阻断回答。
SUPPORT_UNIT_RECALL = 0.4
SUPPORT_MIN_UNITS = 3
SUPPORT_LONG_UNIT = 4

# ⚠️ 只认 1~3 位编号：证据数远小于 1000，而公文编号里的年份是 4 位
#    （「桂电教[2019]36号」）。若把 [2019] 当角标剥掉，句子只剩 `36`，
#    片段原文归一化后却是 `201936` —— 数字校验必然误报。
_MARKER_RE = re.compile(r"\[(\d{1,3})\]")
# ⚠️ **分号也算分句符**：法条式的长句常用「；」并列多个条件，各条件分别引用
#    不同片段。按整句取数字时，后一句引用的「70」会把前一句的 [1] 判成
#    number_mismatch，角标误标。分号处切开后，数字校验只覆盖该引用所在的分句。
_SENTENCE_RE = re.compile(r"[^。！？\n；;]*[。！？\n；;]?")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ALPHA_RE = re.compile(r"[A-Za-z]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
# 版本年号（「2025 年修订」）是文档元数据，不是事实数字 —— 后面的页码片段
# 不会重复标题，拿它跟单个片段比对必然误报。
_VERSION_YEAR_RE = re.compile(r"((?:19|20)\d{2})年(?:修订|版)")

Reason = Literal["no_overlap", "number_mismatch", "weak_overlap"]


def normalize(text: str) -> str:
    """NFKC 后只保留中文/字母/数字，去掉空白、标点、符号。"""
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if ch.isalnum())


def content_units(text: str) -> set[str]:
    """中文二元组 + 英文 token（≥2）+ 数字（任意长度）。"""
    normalized = normalize(text)
    units: set[str] = set()
    for run in _CJK_RUN_RE.findall(normalized):
        units.update(run[i:i + 2] for i in range(len(run) - 1))
    for token in _ALPHA_RE.findall(normalized):
        if len(token) >= 2:
            units.add(token.lower())
    units.update(_NUMBER_RE.findall(normalized))
    return units


def _cjk_ngrams(text: str, n: int = SUPPORT_LONG_UNIT) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(normalize(text)):
        grams.update(run[i:i + n] for i in range(len(run) - n + 1))
    return grams


def _has_long_common(sentence: str, chunk: str) -> bool:
    """公共片段用 4-gram 集合判断，不用 LCS（避免 O(n²)）。"""
    return bool(_cjk_ngrams(sentence) & _cjk_ngrams(chunk))


def _gate_numbers(text: str) -> list[str]:
    """参与数字校验的数字；版本年号不算。"""
    normalized = normalize(_MARKER_RE.sub("", text))
    metadata = set(_VERSION_YEAR_RE.findall(normalized))
    return [n for n in _NUMBER_RE.findall(normalized) if n not in metadata]


def _supports(sentence: str, chunk_text: str,
              cited_numbers: set[str]) -> tuple[bool, float, Reason]:
    # ⚠️ 必须剥掉 [n] 再取数字/单元：归一化会去掉方括号，让 [1][2] 粘成数字 “12”，
    #    于是每一句带两个引用的结论都会被判成「数字不在原文」。
    plain = _MARKER_RE.sub("", sentence)
    sentence_units = content_units(plain)
    if not sentence_units:
        return False, 0.0, "no_overlap"

    chunk_units = content_units(chunk_text)
    recall = len(sentence_units & chunk_units) / len(sentence_units)
    numbers = _gate_numbers(sentence)
    # ⚠️ 数字按**该分句全部被引片段的并集**校验，不按单个片段：
    #    一个分句常同时引用跨页的两个片段，数字只出现在其中一段
    #    （如「本科学位…[1]；学分为70[2]」合并引用），按单段判必然误报。
    number_ok = all(n in cited_numbers for n in numbers)
    long_common = _has_long_common(plain, chunk_text)
    supported = number_ok and (recall >= SUPPORT_UNIT_RECALL or long_common)

    if not number_ok:
        reason: Reason = "number_mismatch"
    elif recall < SUPPORT_UNIT_RECALL and not long_common:
        reason = "weak_overlap" if numbers else "no_overlap"
    else:
        reason = "no_overlap"
    return supported, round(recall, 4), reason


def check_citation_support(
    answer: str, evidence: list[Chunk],
) -> tuple[list[UnsupportedClaim], list[UnsupportedMarker], int]:
    """返回 `(unsupported_citations, unsupported_markers, supported_claims)`。

    - 每条被引片段**单独判定**；失败的编号即使句子整体 supported 也进
      `unsupported_markers`（前端角标提示）。
    - 数字按**该分句全部被引片段的并集**校验，单段不必包含全部数字；
      版本年号（「2025 年修订」）不参与数字比对。
    - 句子级 `unsupported_citations` 只在**所有**被引片段都不支持时生成。
    - 内容单元少于 `SUPPORT_MIN_UNITS` 的句子不判（保守，避免误标）。
    """
    answer = answer or ""
    upper = len(evidence)
    claims: list[UnsupportedClaim] = []
    markers: list[UnsupportedMarker] = []
    supported_claims = 0

    for match in _SENTENCE_RE.finditer(answer):
        raw = match.group(0)
        sentence = raw.strip()
        if not sentence or not is_conclusion_sentence(sentence):
            continue

        legal = list(dict.fromkeys(
            int(m) for m in _MARKER_RE.findall(sentence) if 1 <= int(m) <= upper))
        if not legal:
            continue
        if len(content_units(_MARKER_RE.sub("", sentence))) < SUPPORT_MIN_UNITS:
            continue

        # 该分句全部被引片段的数字并集 —— 数字只要求出现在其中任一段
        cited_numbers = {
            n
            for marker in legal
            for n in _NUMBER_RE.findall(normalize(evidence[marker - 1].text or ""))
        }

        failures: list[UnsupportedMarker] = []
        best_score = 0.0
        best_reason: Reason = "no_overlap"
        any_supported = False
        for marker in legal:
            chunk = evidence[marker - 1]
            ok, score, reason = _supports(sentence, chunk.text or "", cited_numbers)
            if ok:
                any_supported = True
            else:
                failures.append(UnsupportedMarker(
                    marker=marker, chunk_id=chunk.chunk_id,
                    reason=reason, score=score))
            if score > best_score:
                best_score, best_reason = score, reason
        markers.extend(failures)

        if any_supported:
            supported_claims += 1
            continue

        leading = len(raw) - len(raw.lstrip())
        start = match.start() + leading
        claims.append(UnsupportedClaim(
            sentence=sentence,
            char_start=start,
            char_end=start + len(sentence),
            markers=legal,
            unsupported_markers=[m.marker for m in failures],
            reason=best_reason,
            score=best_score,
            cited_chunk_ids=[evidence[m - 1].chunk_id for m in legal],
        ))

    return claims, markers, supported_claims
