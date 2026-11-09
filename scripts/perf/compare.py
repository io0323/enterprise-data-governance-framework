#!/usr/bin/env python
"""負荷試験の結果を、基準値 (perf/baseline.json) と性能目標と比べる.

    python scripts/perf/compare.py perf/results/x.json [--baseline perf/baseline.json] [--threshold 0.30]

p95 が基準値より threshold 以上悪化したら失敗にする。監査の書き込みは 1 秒あたりの件数の低下で判定する。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLD = 0.30
THROUGHPUT_SCENARIOS = ("audit_write",)  # 遅延ではなく件数で判定する
MAX_ERROR_RATE = 0.01
# 確定した性能目標 (PostgreSQL、資産 10 万件、p95)
TARGET_P95_MS = {"search": 300, "asset_detail": 200, "impact": 1000, "rag_authorize": 150, "contract_check": 1000, "inbox": 500, "products": 2000}
TARGET_MIN_RPS = {"audit_write": 200}


def regressions(results: Mapping[str, Any], baseline: Mapping[str, Any], threshold: float = DEFAULT_THRESHOLD) -> list[str]:
    """基準値からの悪化と、エラーの増加を列挙する。基準値に無いシナリオは比べない。"""
    found = []
    current = results["scenarios"]
    for name, base in baseline["scenarios"].items():
        row = current.get(name)
        if row is None:
            found.append(f"{name}: 結果がありません")
            continue
        if row["error_rate"] > MAX_ERROR_RATE:
            found.append(f"{name}: エラー率 {row['error_rate']:.2%}")
        if name in THROUGHPUT_SCENARIOS:
            if row["rps"] <= base["rps"] * (1 - threshold):
                found.append(f"{name}: {base['rps']} 件/秒 → {row['rps']} 件/秒 ({threshold:.0%} 以上の低下)")
        elif row["p95_ms"] >= base["p95_ms"] * (1 + threshold):
            found.append(f"{name}: p95 {base['p95_ms']}ms → {row['p95_ms']}ms ({threshold:.0%} 以上の悪化)")
    return found


def target_misses(results: Mapping[str, Any]) -> list[str]:
    misses = []
    for name, target in TARGET_P95_MS.items():
        row = results["scenarios"].get(name)
        if row is not None and row["p95_ms"] > target:
            misses.append(f"{name}: p95 {row['p95_ms']}ms > 目標 {target}ms")
    for name, target in TARGET_MIN_RPS.items():
        row = results["scenarios"].get(name)
        if row is not None and row["rps"] < target:
            misses.append(f"{name}: {row['rps']} 件/秒 < 目標 {target} 件/秒")
    return misses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / "perf" / "baseline.json")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    for line in target_misses(results):
        print(f"目標未達: {line}")
    found = regressions(results, baseline, args.threshold)
    for line in found:
        print(f"悪化: {line}", file=sys.stderr)
    print("OK" if not found else f"{len(found)} 件の悪化")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
