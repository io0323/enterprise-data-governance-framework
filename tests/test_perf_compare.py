"""Phase 14a: 負荷試験の結果と基準値の比較."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("perf_compare", ROOT / "scripts" / "perf" / "compare.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


def _row(p95=100.0, rps=500.0, error_rate=0.0):
    return {"p50_ms": p95 / 2, "p95_ms": p95, "p99_ms": p95 * 2, "rps": rps, "error_rate": error_rate, "requests": 1000, "users": 5}


BASE = {"scenarios": {"search": _row(100), "audit_write": _row(50, rps=300)}}


@pytest.mark.parametrize("p95,expected", [(100, 0), (129, 0), (130, 1), (200, 1), (50, 0)])
def test_detects_30_percent_latency_regression(p95, expected):
    current = {"scenarios": {"search": _row(p95), "audit_write": _row(50, rps=300)}}
    found = compare.regressions(current, BASE)
    assert len(found) == expected and (not found or "search" in found[0])


@pytest.mark.parametrize("rps,expected", [(300, 0), (211, 0), (210, 1), (100, 1), (900, 0)])
def test_throughput_scenario_uses_rate_not_latency(rps, expected):
    current = {"scenarios": {"search": _row(100), "audit_write": _row(9999, rps=rps)}}  # 遅延の悪化は見ない
    assert len(compare.regressions(current, BASE)) == expected


def test_missing_scenario_and_errors_fail():
    assert "結果がありません" in compare.regressions({"scenarios": {"search": _row(100)}}, BASE)[0]
    errors = compare.regressions({"scenarios": {"search": _row(100, error_rate=0.02), "audit_write": _row(50, rps=300)}}, BASE)
    assert "エラー率" in errors[0]


def test_threshold_is_configurable():
    current = {"scenarios": {"search": _row(115), "audit_write": _row(50, rps=300)}}
    assert compare.regressions(current, BASE, 0.3) == [] and len(compare.regressions(current, BASE, 0.1)) == 1


def test_target_misses():
    ok = {"scenarios": {"search": _row(300), "audit_write": _row(10, rps=200)}}
    assert compare.target_misses(ok) == []
    bad = {"scenarios": {"search": _row(301), "audit_write": _row(10, rps=199)}}
    assert len(compare.target_misses(bad)) == 2


def test_main_exit_codes(tmp_path, capsys):
    base = tmp_path / "base.json"
    base.write_text(json.dumps(BASE), encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(BASE), encoding="utf-8")
    assert compare.main([str(good), "--baseline", str(base)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scenarios": {"search": _row(500), "audit_write": _row(50, rps=300)}}), encoding="utf-8")
    assert compare.main([str(bad), "--baseline", str(base)]) == 1
    assert "悪化" in capsys.readouterr().err


def test_committed_baseline_covers_every_scenario():
    """perf.yml は基準値が無いと比較を省くので、基準値の有無と網羅はここで確認する。"""
    baseline = json.loads((Path(__file__).resolve().parents[1] / "perf" / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["profile"] == "standard"
    assert set(baseline["scenarios"]) == {"search", "asset_detail", "impact", "rag_authorize", "contract_check", "inbox", "products", "audit_write"}
    assert all(row["requests"] > 0 and row["error_rate"] == 0 for row in baseline["scenarios"].values())
