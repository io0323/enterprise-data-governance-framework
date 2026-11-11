"""§7 Discovery: ソース (namespace) とドメインの対応表 (policies/discovery/*.yaml).

MDM のエンティティ定義と同じく、起動時にディスクから読み込んで検証する (不正なら起動を止める)。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from edgf.config import settings


class DiscoveryRulesError(ValueError):
    pass


@dataclass(frozen=True)
class SourceRule:
    namespace: str  # シェル形式のパターン
    domain: str
    steward: str


def load_rules(directory: Path | None = None) -> list[SourceRule]:
    base = Path(directory or Path(settings.policy_dir) / "discovery")
    rules: list[SourceRule] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise DiscoveryRulesError(f"{path.name}: YAML を解釈できません") from e
        if doc.get("version") != 1 or not isinstance(doc.get("sources"), list):
            raise DiscoveryRulesError(f"{path.name}: version: 1 と sources の一覧が必要です")
        for i, row in enumerate(doc["sources"]):
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("namespace", "domain", "steward")):
                raise DiscoveryRulesError(f"{path.name}: sources[{i}] には namespace、domain、steward が必要です")
            rules.append(SourceRule(row["namespace"].strip(), row["domain"].strip(), row["steward"].strip()))
    return rules


_cache: list[SourceRule] | None = None


def get_rules(reload: bool = False) -> list[SourceRule]:
    global _cache
    if _cache is None or reload:
        _cache = load_rules()
    return _cache


def rule_for(namespace: str) -> SourceRule | None:
    """上から順に照合し、最初に一致した行を返す。"""
    return next((r for r in get_rules() if fnmatch.fnmatchcase(namespace, r.namespace)), None)
