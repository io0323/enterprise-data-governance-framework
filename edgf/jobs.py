"""§16 / §19 ジョブ基盤: 外部の依存を追加しない定期実行.

    python -m edgf.jobs list
    python -m edgf.jobs run <job>      # 外部の cron や CronJob から呼ぶ
    python -m edgf.jobs serve          # 標準ライブラリだけの常駐ループ (任意)

各ジョブは冪等で、DB のリースによって同時に 1 つしか実行されない。実行の記録は job_runs に残す。
間隔と有効・無効は EDGF_JOB_<NAME>_INTERVAL_SECONDS / EDGF_JOB_<NAME>_ENABLED で指定する。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.models import JobLease, JobRun, PolicyBundle, utcnow
from edgf.observability import configure_logging

log = logging.getLogger("edgf.jobs")
ACTOR = "system:jobs"
OVERDUE_FACTOR = 3  # 間隔の 3 倍を過ぎても成功していなければ「期限を大きく過ぎた」とみなす
HOUR, DAY = 3600, 86400


def _issues(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import issues
    return {"issues_escalated": [i.id for i in issues.escalate_overdue(db, actor)]}


def _erasure(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import erasure
    return erasure.escalate_overdue(db, actor)


def _mdm(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import mdm
    return mdm.escalate_overdue_reviews(db, actor)


def _remediation(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import ai_remediation
    return ai_remediation.escalate_overdue(db, actor)


def _policies(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import policy
    report = policy.evaluate_and_open_issues(db, actor)
    return {"compliance_rate": report["compliance_rate"], "issues_opened": report["issues_opened"]}


def _retention(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import retention
    return retention.detect_due_assets(db, actor)


def _consents(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import retention
    return retention.expire_consents(db, actor)


def _outbox(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import webhooks
    return webhooks.deliver_due()  # 送信の間は DB のトランザクションを持たないよう、自分でセッションを管理する


def _approvals(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import approvals
    return approvals.escalate_overdue(db, actor)


def _reviews(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import reviews
    return reviews.run_campaigns(db, actor)


def _kpi_snapshot(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import kpi
    return kpi.snapshot(db)


def _unused_assets(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import usage
    return usage.open_unused_issues(db)


def _reachability_probe(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import mesh_reachability
    return mesh_reachability.probe_all(db)


def _audit_anchor(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import audit_anchor
    return audit_anchor.create_anchor(db, actor)


def _ui_sessions(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import ui_sessions
    return ui_sessions.cleanup(db)


def _maturity(db: Session, actor: str) -> dict[str, Any]:
    from edgf.services import maturity
    return maturity.run_cycle(db, actor)


@dataclass(frozen=True)
class Job:
    name: str
    description: str
    fn: Callable[[Session, str], Mapping[str, Any]]
    default_interval: int
    applies: Callable[[], bool] = field(default=lambda: True)  # 機能が無効なら、実行が止まっていても警告しない

    def _env(self, suffix: str) -> str | None:
        return os.environ.get(f"EDGF_JOB_{self.name.upper()}_{suffix}")

    @property
    def interval(self) -> int:
        return int(self._env("INTERVAL_SECONDS") or self.default_interval)

    @property
    def enabled(self) -> bool:
        return (self._env("ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")


JOBS: dict[str, Job] = {j.name: j for j in (
    Job("issue_escalate", "SLA 超過の Issue のエスカレーション (§16)", _issues, HOUR),
    Job("erasure_escalate", "消去要求の期限超過の処理 (§12)", _erasure, HOUR),
    Job("mdm_review_escalate", "MDM の裁定キューの期限超過の処理 (§8)", _mdm, HOUR),
    Job("ai_remediation_escalate", "ModelRemediation の期限超過の処理 (§14)", _remediation, HOUR),
    Job("policy_evaluate", "全資産の自動監査と Issue 化 (§13)", _policies, DAY),
    Job("retention_detect", "保持期限の検出と削除の承認申請 (§10.2)", _retention, DAY),
    Job("consent_expire", "保持期間を過ぎた Consent の失効 (§12)", _consents, DAY),
    Job("outbox_deliver", "Outbox の配信 (§16)", _outbox, 60),
    Job("approval_sla", "承認の SLA 超過の処理 (§16)", _approvals, HOUR),
    Job("review_campaigns", "レビューキャンペーンの作成と期限の処理 (§16 / §11)", _reviews, DAY),
    Job("kpi_snapshot", "KPI の日次のスナップショット (§17)", _kpi_snapshot, DAY),
    Job("audit_anchor", "監査チェーンのアンカリング (§13)", _audit_anchor, HOUR, lambda: settings.anchor_enabled),
    Job("maturity_cycle", "ドメイン別の成熟度の評価の定期の起票と、期限超過の Issue (§20)", _maturity, DAY),
    Job("ui_session_cleanup", "期限切れの UI のセッションとログインの途中の状態の削除 (Phase 17)", _ui_sessions, HOUR),
    Job("unused_assets", "利用の無い資産を廃止の候補として Owner に知らせる (§17 Usage)", _unused_assets, DAY),
    Job("reachability_probe", "Control Plane からの Output Port の到達の確認 (§15。EDGF_PROBE_ENABLED のときだけ)", _reachability_probe, HOUR,
        lambda: settings.probe_enabled),
)}


# ---------------------------------------------------------------- リース
def acquire_lease(name: str, holder: str, seconds: int | None = None) -> datetime | None:
    """リースを取る。取れれば有効期限を返す。期限切れのリースは引き継ぐ (異常終了からの回復)。"""
    now = utcnow()
    expires = now + timedelta(seconds=seconds if seconds is not None else settings.job_lease_seconds)
    with SessionLocal() as db:
        taken = db.execute(update(JobLease).where(JobLease.name == name, JobLease.expires_at < now)
                           .values(holder=holder, acquired_at=now, expires_at=expires))
        if taken.rowcount == 1:
            db.commit()
            return expires
        db.add(JobLease(name=name, holder=holder, acquired_at=now, expires_at=expires))
        try:
            db.commit()
        except IntegrityError:  # 他のプロセスが実行中
            db.rollback()
            return None
    return expires


def release_lease(name: str, holder: str) -> None:
    """自分のリースだけを解放する (期限切れで引き継がれたものには触らない)。"""
    with SessionLocal() as db:
        db.execute(update(JobLease).where(JobLease.name == name, JobLease.holder == holder).values(expires_at=utcnow()))
        db.commit()


# ---------------------------------------------------------------- 実行
def _count(result: Mapping[str, Any]) -> int:
    total = 0
    for value in result.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            total += value
        elif isinstance(value, Sequence) and not isinstance(value, str):
            total += len(value)
    return total


def run_job(name: str, *, force: bool = False, holder: str | None = None) -> dict[str, Any]:
    """ジョブを 1 回実行する。無効のものは実行せず (force を除く)、他で実行中なら何もせずに終わる。"""
    job = JOBS[name]
    if not job.enabled and not force:
        return {"job": name, "status": "disabled"}
    holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    expires = acquire_lease(name, holder)
    if expires is None:
        return {"job": name, "status": "skipped", "reason": "他のプロセスが実行中です"}
    with SessionLocal() as db:
        run = JobRun(job=name, holder=holder, lease_expires_at=expires, status="running")
        db.add(run)
        db.commit()
        run_id = run.id
    status, result, error = "succeeded", {}, None
    try:
        with SessionLocal() as db:
            result = json.loads(json.dumps(dict(job.fn(db, ACTOR)), default=str))
            db.commit()
    except Exception as e:  # noqa: BLE001 - ジョブの失敗は記録して次の実行に任せる
        status, error = "failed", type(e).__name__
        log.exception("job failed", extra={"fields": {"job": name}})
    with SessionLocal() as db:
        run = db.get(JobRun, run_id)
        run.status, run.finished_at, run.result, run.count, run.error = status, utcnow(), result, _count(result), error
        db.commit()
    release_lease(name, holder)
    return {"job": name, "status": status, "count": _count(result), "result": result, "error": error}


# ---------------------------------------------------------------- 状況
def status_of(db: Session, job: Job) -> dict[str, Any]:
    runs = db.scalars(select(JobRun).where(JobRun.job == job.name, JobRun.status != "running").order_by(JobRun.id.desc()).limit(20)).all()
    last = runs[0] if runs else None
    success = db.scalar(select(func.max(JobRun.finished_at)).where(JobRun.job == job.name, JobRun.status == "succeeded"))
    failures = 0
    for run in runs:
        if run.status != "failed":
            break
        failures += 1
    return {"job": job.name, "description": job.description, "enabled": job.enabled, "interval_seconds": job.interval,
            "last_started_at": last.started_at if last else None, "last_finished_at": last.finished_at if last else None,
            "last_status": last.status if last else None, "last_count": last.count if last else None,
            "last_error": last.error if last else None, "last_success_at": success, "consecutive_failures": failures}


def _is_overdue(job: Job, last_success: datetime | None, installed_at: datetime, now: datetime) -> bool:
    """最後の成功 (一度も成功していなければ導入の日時) から、間隔の 3 倍を過ぎたか。"""
    return job.enabled and job.applies() and now - (last_success or installed_at) > timedelta(seconds=job.interval * OVERDUE_FACTOR)


def overdue_jobs(now: datetime | None = None) -> list[str]:
    """/ready の警告に使う。DB に届かなければ判定しない (/ready の database が示す)。"""
    now = now or utcnow()
    try:
        with SessionLocal() as db:
            installed = db.scalar(select(func.min(PolicyBundle.created_at)))
            if installed is None:
                return []
            success = dict(db.execute(select(JobRun.job, func.max(JobRun.finished_at)).where(JobRun.status == "succeeded").group_by(JobRun.job)).all())
    except SQLAlchemyError as e:
        log.warning("job status check skipped", extra={"fields": {"error": type(e).__name__}})
        return []
    return [j.name for j in JOBS.values() if _is_overdue(j, success.get(j.name), installed, now)]


# ---------------------------------------------------------------- 常駐ループ
def due_jobs(now: datetime | None = None) -> list[Job]:
    now = now or utcnow()
    with SessionLocal() as db:
        started = dict(db.execute(select(JobRun.job, func.max(JobRun.started_at)).group_by(JobRun.job)).all())
    return [j for j in JOBS.values() if j.enabled and (j.name not in started or now - started[j.name] >= timedelta(seconds=j.interval))]


def serve(stop: threading.Event | None = None, *, tick_seconds: float = 30, max_ticks: int | None = None) -> None:
    """標準ライブラリだけの常駐ループ。複数のプロセスで動かしてもリースで重複しない。"""
    stop = stop or threading.Event()
    ticks = 0
    while not stop.is_set():
        for job in due_jobs():
            if stop.is_set():
                break
            log.info("job finished", extra={"fields": {k: v for k, v in run_job(job.name).items() if k in ("job", "status", "count", "error")}})
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return
        stop.wait(tick_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m edgf.jobs", description="定期実行のジョブ")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="ジョブの一覧")
    run = commands.add_parser("run", help="ジョブを 1 回実行する")
    run.add_argument("job")
    run.add_argument("--force", action="store_true", help="無効のジョブも実行する")
    loop = commands.add_parser("serve", help="常駐ループ (任意)")
    loop.add_argument("--tick", type=float, default=30, help="確認の間隔 (秒)")
    args = parser.parse_args(argv)
    configure_logging(settings.log_level, settings.log_format)
    if args.command == "list":
        for job in JOBS.values():
            print(f"{job.name}\t{job.interval}s\t{'enabled' if job.enabled else 'disabled'}\t{job.description}")
        return 0
    if args.command == "serve":
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        serve(stop, tick_seconds=args.tick)
        return 0
    if args.job not in JOBS:
        print(f"unknown job: {args.job} (python -m edgf.jobs list で確認できます)", file=sys.stderr)
        return 2
    outcome = run_job(args.job, force=args.force)
    print(json.dumps(outcome, ensure_ascii=False, default=str))
    return 1 if outcome["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
