"""Phase 14a: 負荷試験の結果と基準値の比較."""
from __future__ import annotations

import ast
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


class FakeRunner:
    """シナリオごとに決めた順で結果を返す。実際の Locust は動かさない。"""

    def __init__(self, **sequences):
        self.sequences = {k: list(v) for k, v in sequences.items()}
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return self.sequences[name].pop(0)


def _judge(runner, names=("search", "audit_write"), baseline=BASE, **kw):
    results = compare.measure_with_retries(runner, names, baseline, **kw)
    return results, compare.regressions(results, baseline)


GOOD_AUDIT = [_row(50, rps=300)]


def test_regression_only_in_first_attempt_passes():
    runner = FakeRunner(search=[_row(500), _row(100)], audit_write=GOOD_AUDIT)
    results, found = _judge(runner)
    assert found == [] and results["retried"] == ["search"]
    assert results["scenarios"]["search"]["p95_ms"] == 100
    assert [r["p95_ms"] for r in results["attempts"]["search"]] == [500, 100]  # 全回を残す
    assert runner.calls.count("search") == 2  # 良い回が出たら 3 回目は測らない
    assert runner.calls.count("audit_write") == 1  # 悪化していないシナリオは再計測しない


def test_regression_in_all_three_attempts_fails():
    runner = FakeRunner(search=[_row(500), _row(400), _row(450)], audit_write=GOOD_AUDIT)
    results, found = _judge(runner)
    assert len(found) == 1 and "search" in found[0] and "400" in found[0]  # 最も良い回で判定する
    assert runner.calls.count("search") == 3 and len(results["attempts"]["search"]) == 3


def test_recovery_on_third_attempt_passes():
    runner = FakeRunner(search=[_row(500), _row(400), _row(120)], audit_write=GOOD_AUDIT)
    results, found = _judge(runner)
    assert found == [] and results["scenarios"]["search"]["p95_ms"] == 120


@pytest.mark.parametrize("rates, passes, best", [
    ([100, 300], True, 300),           # 1 回目だけ低い
    ([100, 150, 215], True, 215),      # 3 回目で回復
    ([100, 150, 120], False, 150),     # 3 回とも低い。最も多い件数で判定する
])
def test_throughput_metric_takes_the_highest_rate(rates, passes, best):
    # p95 は判定に使わないので、遅く見える回でも件数が多ければ選ぶ
    runner = FakeRunner(search=[_row(100)], audit_write=[_row(9000 - r, rps=r) for r in rates])
    results, found = _judge(runner)
    assert (found == []) is passes and results["scenarios"]["audit_write"]["rps"] == best
    assert results["retried"] == ["audit_write"]


def test_threshold_stays_at_30_percent():
    assert compare.DEFAULT_THRESHOLD == 0.30 and compare.MAX_RETRIES == 2
    runner = FakeRunner(search=[_row(129)], audit_write=GOOD_AUDIT)
    assert _judge(runner)[0]["retried"] == []
    runner = FakeRunner(search=[_row(130), _row(130), _row(130)], audit_write=GOOD_AUDIT)
    assert len(_judge(runner)[1]) == 1


def test_fast_attempt_with_errors_is_not_chosen():
    """エラーで速く返っただけの回を、最も良い回として選ばない。"""
    runner = FakeRunner(search=[_row(500), _row(1, error_rate=0.5), _row(450)], audit_write=GOOD_AUDIT)
    results, found = _judge(runner)
    assert results["scenarios"]["search"]["p95_ms"] == 450 and len(found) == 1


def test_errors_in_every_attempt_fail():
    runner = FakeRunner(search=[_row(10, error_rate=0.5) for _ in range(3)], audit_write=GOOD_AUDIT)
    results, found = _judge(runner)
    assert runner.calls.count("search") == 3 and "エラー率" in found[0]


def test_no_baseline_means_no_retries():
    runner = FakeRunner(search=[_row(9999)], audit_write=GOOD_AUDIT)
    results = compare.measure_with_retries(runner, ("search", "audit_write"), None)
    assert results["retried"] == [] and runner.calls == ["search", "audit_write"]


def test_scenario_outside_baseline_is_not_retried():
    runner = FakeRunner(extra=[_row(9999)])
    assert compare.measure_with_retries(runner, ("extra",), BASE)["retried"] == []


def test_max_retries_is_configurable():
    runner = FakeRunner(search=[_row(500)], audit_write=GOOD_AUDIT)
    assert compare.measure_with_retries(runner, ("search", "audit_write"), BASE, max_retries=0)["retried"] == []


def test_summary_shows_each_attempt_and_retry(tmp_path):
    probe = {"wal": {"wal_sync_time": 1234.5, "wal_fpi": 9}, "checkpoint": {"checkpoints_timed": 1, "checkpoints_req": 0},
             "disk": {"sda": {"writes": 10, "avg_write_ms": 8.5, "peak_busy_pct": 97.0}}, "cpu": {"iowait_pct": 12.0, "steal_pct": 0.5}, "runner": {"cpu_model": "EPYC", "cpu_count": 4}}
    results = compare.measure_with_retries(FakeRunner(search=[_row(500), _row(100)], audit_write=[{**_row(50, rps=300), "probe": probe}]), ("search", "audit_write"), BASE)
    text = compare.markdown_summary(results, BASE)
    assert "| search | 100ms | 500ms | 100ms | - | あり | 100ms | OK |" in text
    assert "| audit_write | 300 件/秒 | 300 件/秒 | - | - | なし | 300 件/秒 | OK |" in text
    assert "1234.5" in text and "97.0" in text and "EPYC" in text
    path = tmp_path / "summary.md"
    file = tmp_path / "r.json"
    base = tmp_path / "b.json"
    file.write_text(json.dumps(results), encoding="utf-8")
    base.write_text(json.dumps(BASE), encoding="utf-8")
    assert compare.main([str(file), "--baseline", str(base), "--summary", str(path)]) == 0
    assert "性能 CI の結果" in path.read_text(encoding="utf-8")


def test_summary_marks_failures_and_missing_results():
    results = compare.measure_with_retries(FakeRunner(search=[_row(500), _row(400), _row(450)]), ("search",), BASE)
    text = compare.markdown_summary(results, BASE)
    assert "| search | 100ms | 500ms | 400ms | 450ms | あり | 400ms | 悪化 |" in text and "| audit_write | 300 件/秒 | - | - | - | - | - | 結果なし |" in text
    assert "性能目標の未達" in text


def _declared_scenarios():
    """scripts/perf/run.py の一覧。Locust が無い環境でも読めるよう、構文木から取り出す。"""
    tree = ast.parse((ROOT / "scripts" / "perf" / "run.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and node.targets[0].id == "SCENARIOS")


def test_scenario_lists_agree():
    tree = ast.parse((ROOT / "perf" / "locustfile.py").read_text(encoding="utf-8"))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"asset_update", "asset_register"} <= set(_declared_scenarios()) <= functions


WRITE_BASE = {"scenarios": {"asset_update": _row(100, rps=300), "asset_register": _row(100, rps=300)}}


@pytest.mark.parametrize("name", ["asset_update", "asset_register"])
@pytest.mark.parametrize("latencies, passes, best", [
    ([500, 100], True, 100),            # 1 回目だけ悪化
    ([500, 400, 450], False, 400),      # 3 回とも悪化
    ([129], True, 129),                 # 30% 未満は再計測しない
])
def test_write_scenarios_are_judged_on_latency(name, latencies, passes, best):
    """書き込みのシナリオも p95 で判定する (件数は見ない)。"""
    other = "asset_register" if name == "asset_update" else "asset_update"
    runner = FakeRunner(**{name: [_row(p, rps=1) for p in latencies], other: [_row(100, rps=300)]})
    results = compare.measure_with_retries(runner, (name, other), WRITE_BASE)
    assert (compare.regressions(results, WRITE_BASE) == []) is passes
    assert results["scenarios"][name]["p95_ms"] == best and len(results["attempts"][name]) == len(latencies)
    assert f"| {name} |" in compare.markdown_summary(results, WRITE_BASE)


def test_write_scenarios_have_no_slo_target_yet():
    assert not {"asset_update", "asset_register"} & (set(compare.TARGET_P95_MS) | set(compare.TARGET_MIN_RPS))


def test_committed_baseline_covers_every_scenario():
    """perf.yml は基準値が無いと比較を省くので、基準値の有無と網羅はここで確認する。"""
    baseline = json.loads((Path(__file__).resolve().parents[1] / "perf" / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["profile"] == "standard"
    assert set(baseline["scenarios"]) == set(_declared_scenarios())
    assert all(row["requests"] > 0 and row["error_rate"] == 0 for row in baseline["scenarios"].values())
