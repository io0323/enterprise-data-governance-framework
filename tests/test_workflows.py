"""CI のワークフローのファイルの検査 (GitHub は重複したキーを拒否するが、PyYAML は黙って後勝ちにする)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    duplicated = sorted({k for k in keys if keys.count(k) > 1}, key=str)
    if duplicated:
        raise yaml.constructor.ConstructorError(None, None, f"重複したキー: {duplicated}", node.start_mark)
    return loader.construct_mapping(node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_has_no_duplicate_keys(path):
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506  # nosec B506 - SafeLoader の派生
    assert doc["jobs"]


def test_detector_catches_duplicates():
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("jobs:\n  a: 1\n  a: 2\n", Loader=_StrictLoader)  # noqa: S506  # nosec B506


def test_timed_steps_use_the_job_timeout():
    """所要時間の警告の基準 (JOB_TIMEOUT_MINUTES) が、ジョブの timeout-minutes と食い違わないこと。"""
    doc = yaml.load((WORKFLOWS[0].parent / "ci.yml").read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506  # nosec B506
    timed = 0
    for name, job in doc["jobs"].items():
        for step in job["steps"]:
            if "scripts/ci/timed.sh" in str(step.get("run", "")):
                timed += 1
                assert str(step["env"]["JOB_TIMEOUT_MINUTES"]) == str(job["timeout-minutes"]), name
    assert timed == 4  # SQLite、PostgreSQL、Playwright、Data Plane の E2E


def test_timed_script_reports_and_warns(tmp_path):
    import os
    import subprocess
    script = WORKFLOWS[0].parents[2] / "scripts" / "ci" / "timed.sh"
    summary = tmp_path / "summary.md"
    env = {**os.environ, "JOB_TIMEOUT_MINUTES": "1", "CI_WARN_RATIO": "0", "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(["sh", str(script), "対象", "sh", "-c", "sleep 1; exit 3"], env=env, capture_output=True, text=True)
    assert done.returncode == 3  # コマンドの終了コードを返す
    assert "::warning title=CI の所要時間::対象" in done.stdout
    assert "| 対象 |" in summary.read_text(encoding="utf-8") and "| 1 分 |" in summary.read_text(encoding="utf-8")
    quiet = subprocess.run(["sh", str(script), "対象", "true"], env={**env, "CI_WARN_RATIO": "0.6"}, capture_output=True, text=True)
    assert quiet.returncode == 0 and "::warning" not in quiet.stdout
    missing = subprocess.run(["sh", str(script), "対象", "true"], env={k: v for k, v in env.items() if k != "JOB_TIMEOUT_MINUTES"},
                             capture_output=True, text=True)
    assert missing.returncode != 0
