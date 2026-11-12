#!/usr/bin/env python
"""負荷試験の結果を、基準値 (perf/baseline.json) と性能目標と比べる.

    python scripts/perf/compare.py perf/results/x.json [--baseline perf/baseline.json] [--threshold 0.30] [--summary out.md]

p95 が基準値より threshold 以上悪化したら失敗にする。監査の書き込みは 1 秒あたりの件数の低下で判定する。
CI のランナーは一時的に遅くなるので、悪化したシナリオだけを最大 MAX_RETRIES 回まで再計測し (scripts/perf/run.py)、最も良い回で判定する。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THRESHOLD = 0.30
MAX_RETRIES = 2
THROUGHPUT_SCENARIOS = ("audit_write",)  # 遅延ではなく件数で判定する
MAX_ERROR_RATE = 0.01
# 確定した性能目標 (PostgreSQL、資産 10 万件、p95)
TARGET_P95_MS = {"search": 300, "asset_detail": 200, "impact": 1000, "column_impact": 1000, "rag_authorize": 150, "contract_check": 1000, "inbox": 500, "products": 2000}
TARGET_MIN_RPS = {"audit_write": 200}


def _regression(name: str, row: Mapping[str, Any], base: Mapping[str, Any], threshold: float) -> list[str]:
    found = []
    if row["error_rate"] > MAX_ERROR_RATE:
        found.append(f"{name}: エラー率 {row['error_rate']:.2%}")
    if name in THROUGHPUT_SCENARIOS:
        if row["rps"] <= base["rps"] * (1 - threshold):
            found.append(f"{name}: {base['rps']} 件/秒 → {row['rps']} 件/秒 ({threshold:.0%} 以上の低下)")
    elif row["p95_ms"] >= base["p95_ms"] * (1 + threshold):
        found.append(f"{name}: p95 {base['p95_ms']}ms → {row['p95_ms']}ms ({threshold:.0%} 以上の悪化)")
    return found


def regressions(results: Mapping[str, Any], baseline: Mapping[str, Any], threshold: float = DEFAULT_THRESHOLD) -> list[str]:
    """基準値からの悪化と、エラーの増加を列挙する。基準値に無いシナリオは比べない。"""
    found = []
    current = results["scenarios"]
    for name, base in baseline["scenarios"].items():
        row = current.get(name)
        if row is None:
            found.append(f"{name}: 結果がありません")
        else:
            found.extend(_regression(name, row, base, threshold))
    return found


def best_attempt(name: str, attempts: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """最も良い回。エラーで速く返っただけの回を選ばないよう、エラー率が許容内の回を優先する。"""
    rows = list(attempts)
    candidates = [r for r in rows if r["error_rate"] <= MAX_ERROR_RATE] or rows
    if name in THROUGHPUT_SCENARIOS:
        return max(candidates, key=lambda r: r["rps"])
    return min(candidates, key=lambda r: r["p95_ms"])


def measure_with_retries(measure: Callable[[str], dict[str, Any]], names: Iterable[str], baseline: Mapping[str, Any] | None,
                         threshold: float = DEFAULT_THRESHOLD, max_retries: int = MAX_RETRIES) -> dict[str, Any]:
    """全シナリオを 1 回ずつ測り、基準値より悪化したものだけを再計測する。良い回が出たらそのシナリオの再計測をやめる。"""
    attempts: dict[str, list[dict[str, Any]]] = {name: [measure(name)] for name in names}
    bases = (baseline or {}).get("scenarios", {})

    def pending() -> list[str]:
        return [n for n, rows in attempts.items() if n in bases and _regression(n, best_attempt(n, rows), bases[n], threshold)]

    for _ in range(max_retries):
        for name in pending():
            attempts[name].append(measure(name))
    return {"scenarios": {n: dict(best_attempt(n, rows)) for n, rows in attempts.items()}, "attempts": attempts,
            "retried": [n for n, rows in attempts.items() if len(rows) > 1]}


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


def _metric(name: str, row: Mapping[str, Any]) -> str:
    return f"{row['rps']} 件/秒" if name in THROUGHPUT_SCENARIOS else f"{row['p95_ms']:g}ms"


def _probe_rows(results: Mapping[str, Any]) -> list[str]:
    lines = []
    for name, rows in results.get("attempts", {}).items():
        for number, row in enumerate(rows, start=1):
            probe = row.get("probe")
            if not probe:
                continue
            wal, checkpoint, cpu = probe.get("wal", {}), probe.get("checkpoint", {}), probe.get("cpu", {})
            disks = probe.get("disk", {})
            disk = max(disks.values(), key=lambda d: d["writes"], default={})
            lines.append(f"| {name} {number} 回目 | {row['rps']} | {row['p95_ms']:g} | {wal.get('wal_sync_time', '-')} | {wal.get('wal_fpi', '-')} | "
                         f"{checkpoint.get('checkpoints_timed', 0) + checkpoint.get('checkpoints_req', 0) if checkpoint else '-'} | "
                         f"{disk.get('avg_write_ms', '-')} | {disk.get('peak_busy_pct', '-')} | {cpu.get('iowait_pct', '-')} | {cpu.get('steal_pct', '-')} |")
    return lines


def markdown_summary(results: Mapping[str, Any], baseline: Mapping[str, Any], threshold: float = DEFAULT_THRESHOLD) -> str:
    """job summary の本文。各回の結果、再計測の有無、判定を示す。"""
    attempts = results.get("attempts", {})
    lines = ["## 性能 CI の結果", "", f"基準値より {threshold:.0%} 以上悪化したシナリオだけを再計測し、最も良い回で判定します。", "",
             "| シナリオ | 基準値 | 1 回目 | 2 回目 | 3 回目 | 再計測 | 判定値 | 結果 |", "|---|---|---|---|---|---|---|---|"]
    for name, base in baseline["scenarios"].items():
        row = results["scenarios"].get(name)
        if row is None:
            lines.append(f"| {name} | {_metric(name, base)} | - | - | - | - | - | 結果なし |")
            continue
        rows = attempts.get(name, [row])
        cells = [_metric(name, rows[i]) if i < len(rows) else "-" for i in range(1 + MAX_RETRIES)]
        verdict = "悪化" if _regression(name, row, base, threshold) else "OK"
        lines.append(f"| {name} | {_metric(name, base)} | {' | '.join(cells)} | {'あり' if len(rows) > 1 else 'なし'} | {_metric(name, row)} | {verdict} |")
    misses = target_misses(results)
    if misses:
        lines += ["", "性能目標の未達(失敗にはしません): " + "、".join(misses)]
    probes = _probe_rows(results)
    runner = next((r["probe"]["runner"] for rows in attempts.values() for r in rows if r.get("probe", {}).get("runner")), None)
    if runner:
        lines += ["", f"ランナー: {runner['cpu_model']}({runner['cpu_count']} 論理 CPU)"]
    if probes:
        lines += ["", "### WAL、checkpoint、ディスク", "", "| 回 | 件/秒 | p95 ms | WAL sync ms | WAL FPI | checkpoint 完了数 | 書き込み待ち ms | 最大 busy % | iowait % | steal % |",
                  "|---|---|---|---|---|---|---|---|---|---|", *probes]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / "perf" / "baseline.json")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--summary", type=Path, help="結果の Markdown を追記する (CI では $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(markdown_summary(results, baseline, args.threshold))
    for name in results.get("retried", []):
        print(f"再計測: {name} ({len(results['attempts'][name])} 回)")
    for line in target_misses(results):
        print(f"目標未達: {line}")
    found = regressions(results, baseline, args.threshold)
    for line in found:
        print(f"悪化: {line}", file=sys.stderr)
    print("OK" if not found else f"{len(found)} 件の悪化")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
