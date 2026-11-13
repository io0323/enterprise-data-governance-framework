"""§3 Data Classification / §12 PII: 分類の提案の辞書規則 (policies/classification/rules.yaml。Phase 19).

カラム名と説明文 (と Glossary の用語) に対して照合する。英語の語は語の並び (snake_case / camelCase / 記号で区切る) で、
日本語の語は部分一致で照合する。提案は保護を強める方向だけなので、pii と cde は true だけを書ける。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import PiiCategory

RULES_FILE = "classification/rules.yaml"
FIELDS = ("pii", "pii_category", "sensitivity", "cde")
_ID = re.compile(r"^[A-Z][A-Z0-9-]{1,39}$")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT = re.compile(r"[^0-9a-z]+")


class RulesInvalid(ValueError):
    """辞書規則の宣言の不備 (ロード時のエラー)。"""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def tokens(text: str) -> list[str]:
    """英語の語に分ける (camelCase、snake_case、記号)。"""
    return [t for t in _SPLIT.split(_CAMEL.sub(r"\1 \2", normalize(text)).lower()) if t]


def is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    patterns: tuple[str, ...]
    suggest: Mapping[str, Any]
    confidence: float

    def matches(self, text: str) -> str | None:
        """一致したパターン (無ければ None)。英語は語の並びで、日本語は部分一致で照合する。"""
        words = tokens(text)
        lowered = normalize(text).lower()
        for pattern in self.patterns:
            if is_ascii(pattern):
                needle = tokens(pattern)
                if needle and any(words[i:i + len(needle)] == needle for i in range(len(words) - len(needle) + 1)):
                    return pattern
            elif normalize(pattern).lower() in lowered:
                return pattern
        return None


@dataclass(frozen=True)
class Rules:
    items: tuple[Rule, ...] = ()
    digest: str | None = None


def _rule(raw: Any, where: str) -> Rule:
    if not isinstance(raw, Mapping) or set(raw) - {"id", "description", "patterns", "suggest", "confidence"}:
        raise RulesInvalid(f"{where}: id / description / patterns / suggest / confidence だけを持つオブジェクトにしてください")
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID.match(rid):
        raise RulesInvalid(f"{where}.id は英大文字で始まる英大文字・数字・- の 2〜40 文字にしてください")
    patterns = raw.get("patterns")
    if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and 0 < len(p.strip()) <= 50 for p in patterns):
        raise RulesInvalid(f"{where}.patterns は 1〜50 文字の文字列の配列にしてください")
    suggest = raw.get("suggest")
    if not isinstance(suggest, Mapping) or not suggest or set(suggest) - set(FIELDS):
        raise RulesInvalid(f"{where}.suggest は {list(FIELDS)} の一部を持つオブジェクトにしてください")
    if "pii" in suggest and suggest["pii"] is not True or "cde" in suggest and suggest["cde"] is not True:
        raise RulesInvalid(f"{where}.suggest: pii と cde は true だけを書けます (保護を弱める提案はしない)")
    if "pii_category" in suggest:
        if suggest["pii_category"] not in {c.value for c in PiiCategory}:
            raise RulesInvalid(f"{where}.suggest.pii_category は {[c.value for c in PiiCategory]} のいずれかです")
        if suggest.get("pii") is not True:
            raise RulesInvalid(f"{where}.suggest: pii_category には pii: true が必要です")
    if "sensitivity" in suggest and suggest["sensitivity"] not in SENSITIVITY_LEVELS:
        raise RulesInvalid(f"{where}.suggest.sensitivity は {list(SENSITIVITY_LEVELS)} のいずれかです")
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 < confidence <= 1:
        raise RulesInvalid(f"{where}.confidence は 0 より大きく 1 以下の数値にしてください")
    return Rule(rid, str(raw.get("description") or ""), tuple(p.strip() for p in patterns), dict(suggest), float(confidence))


def parse(text: str) -> Rules:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise RulesInvalid(f"{RULES_FILE}: YAML を解釈できません") from e
    if not isinstance(doc, Mapping) or set(doc) - {"rules"} or not isinstance(doc.get("rules", []), list):
        raise RulesInvalid(f"{RULES_FILE}: rules (配列) だけを持つオブジェクトにしてください")
    items: list[Rule] = []
    for i, raw in enumerate(doc.get("rules", [])):
        rule = _rule(raw, f"{RULES_FILE}: rules[{i}]")
        if any(r.id == rule.id for r in items):
            raise RulesInvalid(f"{RULES_FILE}: 規則の id が重複しています ({rule.id})")
        items.append(rule)
    return Rules(tuple(items), hashlib.sha256(text.encode()).hexdigest())


def active(db: Any) -> Rules:
    from edgf.services import policy_bundles  # 循環 import 回避

    bundle = policy_bundles.active_bundle(db)
    text = (bundle.files or {}).get(RULES_FILE) if bundle else None
    return parse(text) if text else Rules()
