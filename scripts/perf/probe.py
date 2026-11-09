#!/usr/bin/env python
"""負荷試験の間の WAL、checkpoint、ディスク、CPU の統計を取る (ディスクの詰まりの仮説の検証用).

    python scripts/perf/probe.py enable-timing   # CI の DB で WAL の待ち時間の記録を有効にする

PostgreSQL 16 の checkpoint の統計は pg_stat_bgwriter にある。pg_stat_checkpointer は 17 からで、あれば同じ項目に揃えて読む。
測定を止めないよう、統計が取れなくても例外にせず、結果に error を残す。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any

WAL_SQL = "SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full, wal_write, wal_sync, wal_write_time, wal_sync_time FROM pg_stat_wal"
BGWRITER_SQL = "SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time, checkpoint_sync_time, buffers_checkpoint, buffers_backend_fsync FROM pg_stat_bgwriter"
CHECKPOINTER_SQL = ("SELECT num_timed AS checkpoints_timed, num_requested AS checkpoints_req, write_time AS checkpoint_write_time, "
                    "sync_time AS checkpoint_sync_time, buffers_written AS buffers_checkpoint FROM pg_stat_checkpointer")
WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)$")  # パーティションを数えると二重になる
CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
SAMPLE_SECONDS = 1.0
FLUSH_SECONDS = 11.0  # 待機中のバックエンドの統計は最大 10 秒遅れて反映される


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    """/proc/diskstats の累積値。項目の意味は Linux の Documentation/admin-guide/iostats.rst。"""
    disks = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 14 and WHOLE_DISK.match(parts[2]):
            disks[parts[2]] = {"reads": int(parts[3]), "read_ms": int(parts[6]), "writes": int(parts[7]), "sectors_written": int(parts[9]),
                               "write_ms": int(parts[10]), "busy_ms": int(parts[12]), "queue_ms": int(parts[13])}
    return disks


def parse_cpu(text: str) -> dict[str, int]:
    """/proc/stat の先頭行 (全 CPU の累積)。"""
    values = text.splitlines()[0].split()[1:]
    return {name: int(value) for name, value in zip(CPU_FIELDS, values, strict=False)}


def diff(after: Mapping[str, Any], before: Mapping[str, Any]) -> dict[str, float]:
    return {key: round(float(after[key]) - float(before[key]), 3) for key in after if key in before}


def summarize_disks(before: Mapping[str, Mapping[str, int]], after: Mapping[str, Mapping[str, int]], seconds: float, peak_busy: Mapping[str, float]) -> dict[str, dict[str, float]]:
    """動きのあったディスクごとの要約。avg_write_ms は書き込み 1 回あたりの待ち時間 (iostat の w_await)。"""
    summary = {}
    for name, end in after.items():
        if name not in before:
            continue
        delta = diff(end, before[name])
        if not delta["writes"] and not delta["reads"]:
            continue
        summary[name] = {"writes": delta["writes"], "written_mb": round(delta["sectors_written"] * 512 / 1_048_576, 1),
                         "avg_write_ms": round(delta["write_ms"] / delta["writes"], 2) if delta["writes"] else 0.0,
                         "busy_pct": round(delta["busy_ms"] / (seconds * 1000) * 100, 1) if seconds else 0.0,
                         "peak_busy_pct": peak_busy.get(name, 0.0), "avg_queue": round(delta["queue_ms"] / (seconds * 1000), 2) if seconds else 0.0}
    return summary


def summarize_cpu(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, float]:
    delta = diff(after, before)
    total = sum(delta.values())
    return {f"{key}_pct": round(delta.get(key, 0) / total * 100, 1) for key in ("iowait", "steal", "user", "system")} if total else {}


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as f:
            return f.read()
    except OSError:  # Linux 以外
        return None


def _engine(url: str):
    from sqlalchemy import create_engine
    return create_engine(url, pool_pre_ping=True, isolation_level="AUTOCOMMIT")


def db_snapshot(conn: Any) -> dict[str, dict[str, Any]]:
    from sqlalchemy import text
    has_checkpointer = conn.execute(text("SELECT to_regclass('pg_stat_checkpointer') IS NOT NULL")).scalar_one()
    return {"wal": dict(conn.execute(text(WAL_SQL)).mappings().one()),
            "checkpoint": dict(conn.execute(text(CHECKPOINTER_SQL if has_checkpointer else BGWRITER_SQL)).mappings().one())}


class Probe:
    """with の間の統計を集める。結果は result。"""

    def __init__(self, url: str | None) -> None:
        self.url = url
        self.result: dict[str, Any] = {}
        self._stop = threading.Event()
        self._peak: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        previous = _read("/proc/diskstats")
        while previous is not None and not self._stop.wait(SAMPLE_SECONDS):
            current = _read("/proc/diskstats")
            if current is None:
                return
            start, end = parse_diskstats(previous), parse_diskstats(current)
            for name in end.keys() & start.keys():
                busy = (end[name]["busy_ms"] - start[name]["busy_ms"]) / (SAMPLE_SECONDS * 10)
                self._peak[name] = round(max(self._peak.get(name, 0.0), busy), 1)
            previous = current

    def _db(self) -> dict[str, Any]:
        with _engine(self.url).connect() as conn:
            return db_snapshot(conn)

    def __enter__(self) -> Probe:
        self._started = time.monotonic()
        self._disk, self._cpu = _read("/proc/diskstats"), _read("/proc/stat")
        try:
            self._before = self._db() if self.url else None
        except Exception as e:  # noqa: BLE001 - 統計が取れなくても測定は続ける
            self._before, self.result["db_error"] = None, type(e).__name__
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        seconds = time.monotonic() - self._started
        disk, cpu = _read("/proc/diskstats"), _read("/proc/stat")
        if self._disk is not None and disk is not None:
            self.result["disk"] = summarize_disks(parse_diskstats(self._disk), parse_diskstats(disk), seconds, self._peak)
        if self._cpu is not None and cpu is not None:
            self.result["cpu"] = summarize_cpu(parse_cpu(self._cpu), parse_cpu(cpu))
        if self._before is not None:
            time.sleep(FLUSH_SECONDS)
            try:
                after = self._db()
                self.result["wal"] = diff(after["wal"], self._before["wal"])
                self.result["checkpoint"] = diff(after["checkpoint"], self._before["checkpoint"])
            except Exception as e:  # noqa: BLE001
                self.result["db_error"] = type(e).__name__


def enable_timing(url: str) -> None:
    """track_wal_io_timing は既定で off で、off だと wal_sync_time が 0 のままになる。測定専用の DB にだけ使う。"""
    from sqlalchemy import text
    with _engine(url).connect() as conn:
        conn.execute(text("ALTER SYSTEM SET track_wal_io_timing = on"))
        conn.execute(text("SELECT pg_reload_conf()"))


if __name__ == "__main__":
    if sys.argv[1:] != ["enable-timing"]:
        raise SystemExit(__doc__)
    enable_timing(os.environ["EDGF_DATABASE_URL"])
