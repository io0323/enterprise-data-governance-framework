"""性能 CI の統計の収集 (ディスクの詰まりの仮説の検証用)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("perf_probe", ROOT / "scripts" / "perf" / "probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["perf_probe"] = probe
spec.loader.exec_module(probe)

DISKSTATS = """\
   7       0 loop0 10 0 80 5 0 0 0 0 0 4 5 0 0 0 0
   8       0 sda 100 0 800 50 1000 0 8000 2000 0 900 2500 0 0 0 0
   8       1 sda1 100 0 800 50 1000 0 8000 2000 0 900 2500 0 0 0 0
   8      16 sdb 1 0 8 1 0 0 0 0 0 1 1 0 0 0 0
 259       0 nvme0n1 5 0 40 2 5 0 40 2 0 3 4 0 0 0 0
"""


def test_parse_diskstats_counts_whole_disks_only():
    disks = probe.parse_diskstats(DISKSTATS)
    assert set(disks) == {"sda", "sdb", "nvme0n1"}
    assert disks["sda"] == {"reads": 100, "read_ms": 50, "writes": 1000, "sectors_written": 8000, "write_ms": 2000, "busy_ms": 900, "queue_ms": 2500}


def test_summarize_disks_computes_write_wait():
    before = probe.parse_diskstats(DISKSTATS)
    after = {"sda": {**before["sda"], "writes": 1100, "write_ms": 3000, "sectors_written": 10048, "busy_ms": 5900, "queue_ms": 12500}, "sdb": before["sdb"], "new": before["sda"]}
    summary = probe.summarize_disks(before, after, 10.0, {"sda": 98.0})
    assert summary == {"sda": {"writes": 100.0, "written_mb": 1.0, "avg_write_ms": 10.0, "busy_pct": 50.0, "peak_busy_pct": 98.0, "avg_queue": 1.0}}  # 動きの無いディスクは除く


def test_summarize_disks_reads_only_and_zero_seconds():
    before = probe.parse_diskstats(DISKSTATS)
    after = {"sdb": {**before["sdb"], "reads": 5}}
    assert probe.summarize_disks(before, after, 0, {})["sdb"]["avg_write_ms"] == 0.0


def test_parse_cpu_and_percentages():
    before = probe.parse_cpu("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 1 1 1\n")
    after = probe.parse_cpu("cpu  150 0 110 820 15 0 0 5 0 0\n")
    assert probe.summarize_cpu(before, after) == {"iowait_pct": 15.0, "steal_pct": 5.0, "user_pct": 50.0, "system_pct": 10.0}
    assert probe.summarize_cpu(before, before) == {}


def test_diff_handles_numeric_types_and_missing_keys():
    from decimal import Decimal
    assert probe.diff({"a": Decimal("5.5"), "b": 3, "c": 1}, {"a": 1, "b": 1}) == {"a": 4.5, "b": 2.0}


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def one(self):
        return self.value


class FakeConn:
    def __init__(self, has_checkpointer):
        self.has_checkpointer, self.statements = has_checkpointer, []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        return FakeResult(self.has_checkpointer if "to_regclass" in sql else {"wal_sync_time": 1} if "pg_stat_wal" in sql else {"checkpoints_timed": 2})


@pytest.mark.parametrize("has_checkpointer, view", [(False, "pg_stat_bgwriter"), (True, "pg_stat_checkpointer")])
def test_snapshot_reads_the_checkpoint_view_of_the_server(has_checkpointer, view):
    """pg_stat_checkpointer は PostgreSQL 17 から。16 では pg_stat_bgwriter から読む。"""
    conn = FakeConn(has_checkpointer)
    assert probe.db_snapshot(conn) == {"wal": {"wal_sync_time": 1}, "checkpoint": {"checkpoints_timed": 2}}
    assert view in conn.statements[-1]


def test_probe_never_breaks_the_measurement_without_a_database(monkeypatch):
    monkeypatch.setattr(probe, "SAMPLE_SECONDS", 0.01)
    with probe.Probe("postgresql+psycopg://x:y@127.0.0.1:1/none") as p:
        pass
    assert "db_error" in p.result and "wal" not in p.result


def test_probe_without_url_and_without_proc(monkeypatch):
    monkeypatch.setattr(probe, "_read", lambda path: None)
    with probe.Probe(None) as p:
        pass
    assert p.result == {}


def test_probe_collects_disk_and_cpu(monkeypatch):
    stat = iter(["cpu  1 0 1 8 0 0 0 0\n", "cpu  2 0 2 9 1 0 0 0\n"])
    disk = iter([DISKSTATS, DISKSTATS, DISKSTATS.replace("sda 100 0 800 50 1000 0 8000 2000 0 900", "sda 100 0 800 50 1100 0 8800 2100 0 1900")])

    def read(path):
        return next(stat) if path == "/proc/stat" else next(disk, DISKSTATS.replace("sda 100 0 800 50 1000 0 8000 2000 0 900", "sda 100 0 800 50 1100 0 8800 2100 0 1900"))

    monkeypatch.setattr(probe, "_read", read)
    monkeypatch.setattr(probe, "SAMPLE_SECONDS", 0.01)
    with probe.Probe(None) as p:
        while p._thread.is_alive() and not p._peak:
            pass
    assert p.result["disk"]["sda"]["writes"] == 100 and p.result["cpu"]["iowait_pct"] == 25.0



def test_engine_is_autocommit():
    assert isinstance(probe._engine("sqlite://"), type(create_engine("sqlite://")))
