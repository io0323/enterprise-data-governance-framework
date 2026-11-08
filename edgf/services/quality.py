"""§5 Data Quality Engine.

Control Plane はルール定義とスコアのみ保持する。検査対象レコードは Data Plane から
受け取り、評価後は保持しない (データ本体非保持の原則 §18.2)。
"""
from __future__ import annotations

import operator
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.enums import SEVERITY_WEIGHT, Severity
from edgf.models import QualityRule, QualityRun

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
}


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _check(rule: QualityRule, records: list[dict]) -> tuple[int, int, list[int]]:
    """Returns (evaluated, passed, sample_failed_indices)."""
    col, p, rt = rule.column, rule.params or {}, rule.rule_type
    failed: list[int] = []

    if rt == "unique":
        keys = p.get("columns") or [col]
        tuples = [tuple(r.get(k) for k in keys) for r in records]
        counts = Counter(tuples)
        failed = [i for i, t in enumerate(tuples) if counts[t] > 1]
        return len(records), len(records) - len(failed), failed[:20]

    def ok(r: dict) -> bool | None:  # None = 評価対象外 (null は completeness で扱う)
        v = r.get(col) if col else None
        if rt == "not_null":
            return not _missing(v)
        if rt == "comparison":
            left, right = r.get(p["left"]), r.get(p["right"])
            if _missing(left) or _missing(right):
                return None
            try:
                return _OPS[p.get("op", "==")](left, right)
            except TypeError:
                return False
        if _missing(v):
            return None
        if rt == "regex":
            return re.fullmatch(p["pattern"], str(v)) is not None
        if rt == "range":
            try:
                x = float(v)
            except (TypeError, ValueError):
                return False
            return ("min" not in p or x >= p["min"]) and ("max" not in p or x <= p["max"])
        if rt in ("allowed_values", "referential"):
            return v in set(p.get("values") or p.get("reference_values") or [])
        if rt == "freshness":
            dt = _parse_dt(v)
            return dt is not None and datetime.now(timezone.utc) - dt <= timedelta(hours=p["max_age_hours"])
        if rt == "reference_match":  # accuracy: 検証済ソースとの照合
            key = r.get(p["key"])
            ref = p.get("reference", {})
            return str(key) not in ref or ref[str(key)] == v
        raise ValueError(f"unsupported rule_type: {rt}")

    evaluated = passed = 0
    for i, r in enumerate(records):
        res = ok(r)
        if res is None:
            continue
        evaluated += 1
        if res:
            passed += 1
        else:
            failed.append(i)
    return evaluated, passed, failed[:20]


SUPPORTED_RULE_TYPES = {"not_null", "unique", "regex", "range", "allowed_values", "referential",
                        "freshness", "comparison", "reference_match"}


def validate_rule(rule_type: str, column: str | None, params: dict) -> None:
    if rule_type not in SUPPORTED_RULE_TYPES:
        raise ValueError(f"unsupported rule_type: {rule_type}")
    required = {"regex": ["pattern"], "freshness": ["max_age_hours"], "comparison": ["left", "right"],
                "reference_match": ["key", "reference"]}.get(rule_type, [])
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError(f"params missing: {missing}")
    if rule_type not in ("comparison", "unique") and not column:
        raise ValueError("column is required")
    if rule_type == "unique" and not (column or params.get("columns")):
        raise ValueError("column or params.columns is required")


def evaluate(rules: list[QualityRule], records: list[dict]) -> dict:
    results, weighted, weights = [], 0.0, 0.0
    dim_acc: dict[str, list[float]] = {}
    for rule in rules:
        evaluated, passed, sample = _check(rule, records)
        rate = passed / evaluated if evaluated else 1.0
        w = SEVERITY_WEIGHT[Severity(rule.severity)]
        weighted += rate * w
        weights += w
        dim_acc.setdefault(rule.dimension, []).append(rate)
        results.append({"rule_id": rule.id, "name": rule.name, "dimension": rule.dimension,
                        "severity": rule.severity, "evaluated": evaluated, "passed": passed,
                        "pass_rate": round(rate, 6), "threshold": rule.threshold,
                        "breached": rate < rule.threshold, "failed_samples": sample})
    return {
        "score": round(weighted / weights, 6) if weights else 1.0,
        "dimension_scores": {d: round(sum(v) / len(v), 6) for d, v in dim_acc.items()},
        "results": results,
    }


def latest_run(db: Session, asset_id: str) -> QualityRun | None:
    return db.scalar(select(QualityRun).where(QualityRun.asset_id == asset_id)
                     .order_by(QualityRun.executed_at.desc()).limit(1))
