#!/usr/bin/env python
"""全シナリオを順に Locust で実行し、結果を perf/results/*.json に出力する.

    python scripts/perf/run.py --host http://127.0.0.1:8000 [--seconds 30] [--users 5] [--scenario impact] [--output perf/results/x.json]
                               [--baseline perf/baseline.json] [--max-retries 2] [--probe]

--baseline を指定すると、基準値より悪化したシナリオだけを再計測する。--probe は監査のシナリオの間の WAL、checkpoint、ディスクの統計を記録する。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare  # noqa: E402
from probe import Probe  # noqa: E402

SCENARIOS = ("search", "asset_detail", "impact", "rag_authorize", "contract_check", "inbox", "products", "audit_write")
# 重いシナリオは同時に動かす数を減らす (目標は同時実行時の遅延だが、単一の利用者の遅延と切り分けられるようにする)
USERS = {"products": 2, "audit_write": 16}
PROBED = ("audit_write",)  # 書き込みの詰まりの検証の対象


def run_scenario(name: str, host: str, seconds: int, users: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = str(Path(tmp) / "r")
        count = str(USERS.get(name, users))
        env = {**os.environ, "EDGF_PERF_SCENARIO": name}
        subprocess.run([sys.executable, "-m", "locust", "-f", str(ROOT / "perf" / "locustfile.py"), "--headless", "--host", host, "-u", count, "-r", count, "-t", f"{seconds}s",
                        "--csv", prefix, "--only-summary", "--loglevel", "WARNING", "--exit-code-on-error", "0"], env=env, check=True, capture_output=True)
        with open(f"{prefix}_stats.csv", encoding="utf-8") as f:
            row = next(r for r in csv.DictReader(f) if r["Name"] == name)
    requests = int(row["Request Count"])
    failures = int(row["Failure Count"])
    return {"p50_ms": float(row["50%"]), "p95_ms": float(row["95%"]), "p99_ms": float(row["99%"]), "rps": round(float(row["Requests/s"]), 2),
            "error_rate": round(failures / requests, 4) if requests else 1.0, "requests": requests, "users": int(count)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", default="standard")
    parser.add_argument("--baseline", type=Path, help="基準値。指定すると悪化したシナリオだけを再計測する")
    parser.add_argument("--max-retries", type=int, default=compare.MAX_RETRIES)
    parser.add_argument("--probe", action="store_true", help="監査のシナリオの間の統計を記録する (EDGF_DATABASE_URL を読む)")
    args = parser.parse_args()

    def measure(name: str) -> dict:
        if args.probe and name in PROBED:
            with Probe(os.environ.get("EDGF_DATABASE_URL")) as probe:
                row = run_scenario(name, args.host, args.seconds, args.users)
            row["probe"] = probe.result
        else:
            row = run_scenario(name, args.host, args.seconds, args.users)
        print(name, json.dumps(row), flush=True)
        return row

    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    results = compare.measure_with_retries(measure, args.scenario or SCENARIOS, baseline, max_retries=args.max_retries)
    output = args.output or ROOT / "perf" / "results" / f"{args.profile}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"profile": args.profile, "seconds": args.seconds, **results}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
