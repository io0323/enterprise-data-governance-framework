"""§16 Policy / §20 Level 5: 統治の自己改善 (Phase 19。docs/adr/0001).

次の条件に当てはまる Policy を検出し、Committee に見直しの Issue を起票する (区分 B。Policy そのものは変えない)。
- always_pass     : 直近 N 日 (既定 180 日) のあいだ常に合格している (違反の Issue が無く、いまも適用先がすべて合格)
- never_applied   : 一度も適用されていない (いま適用先が無く、違反の Issue も一度も無い)
- exception_heavy : 例外の申請が閾値 (既定 5 件) を超えている (直近 365 日)
- long_failing    : 不合格が長期間 (既定 90 日) 続いている (未解決の違反の Issue が N 日より前から残っている)

always_pass と never_applied は、Policy Bundle の運用が N 日に満たない環境では判定しない (導入直後の誤検出を避ける)。
見直しの判断と Policy の変更は、従来どおり Policy 変更の承認 (Committee) を経る。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.enums import EscalationLevel
from edgf.models import Issue, PolicyBundle, PolicyException
from edgf.services import autonomy, issues, policy
from edgf.services.autonomy import Candidate, Executor

EXCEPTION_WINDOW_DAYS = 365
LABELS = {"always_pass": "直近 {days} 日のあいだ常に合格しています", "never_applied": "一度も適用されていません",
          "exception_heavy": "例外の申請が {count} 件あります (閾値 {threshold} 件)", "long_failing": "不合格が {days} 日以上続いています"}


def _since_first_bundle(db: Session) -> datetime | None:
    return db.scalar(select(func.min(PolicyBundle.created_at)))


def findings(db: Session, cond: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    kinds = set(cond["findings"])
    report = policy.evaluate_all(db)
    stats: dict[str, dict[str, int]] = {}
    for a in report["assets"]:
        for r in a["results"]:
            s = stats.setdefault(r["policy_id"], {"applicable": 0, "failing": 0})
            s["applicable"] += 1
            s["failing"] += r["status"] == "fail"
    history_ok = (first := _since_first_bundle(db)) is not None and first <= now - timedelta(days=cond["always_pass_days"])
    out = []
    for p in policy.get_policies(db=db):
        if p.target != policy.ASSET:
            continue
        ref = f"policy:{p.id}"
        s = stats.get(p.id, {"applicable": 0, "failing": 0})
        issues_any = db.scalar(select(func.count()).select_from(Issue).where(Issue.source_ref == ref)) or 0
        recent = db.scalar(select(func.count()).select_from(Issue).where(
            Issue.source_ref == ref, Issue.created_at >= now - timedelta(days=cond["always_pass_days"]))) or 0
        if "always_pass" in kinds and history_ok and s["applicable"] and not s["failing"] and not recent:
            out.append({"policy_id": p.id, "finding": "always_pass", "applicable": s["applicable"], "days": cond["always_pass_days"]})
        if "never_applied" in kinds and history_ok and report["assets"] and not s["applicable"] and not issues_any:
            out.append({"policy_id": p.id, "finding": "never_applied", "assets_evaluated": len(report["assets"])})
        if "exception_heavy" in kinds:
            count = db.scalar(select(func.count()).select_from(PolicyException).where(
                PolicyException.policy_id == p.id, PolicyException.created_at >= now - timedelta(days=EXCEPTION_WINDOW_DAYS))) or 0
            if count >= cond["exception_threshold"]:
                out.append({"policy_id": p.id, "finding": "exception_heavy", "count": count, "threshold": cond["exception_threshold"],
                            "window_days": EXCEPTION_WINDOW_DAYS})
        if "long_failing" in kinds:
            oldest = db.scalar(select(func.min(Issue.created_at)).where(Issue.source_ref == ref, Issue.status.in_(issues.OPEN_STATES)))
            if oldest is not None and oldest <= now - timedelta(days=cond["failing_days"]):
                out.append({"policy_id": p.id, "finding": "long_failing", "days": cond["failing_days"], "since": oldest.isoformat(),
                            "failing": s["failing"]})
    return out


def detect_candidates(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    # 同じ Policy の同じ所見は 1 年に 1 回だけ起票する (見直しを終えても条件が続くなら、翌年また知らせる)
    return [Candidate("policy_finding", f"{f['policy_id']}:{f['finding']}", f"{f['policy_id']}:{f['finding']}:{now.year}", None, f)
            for f in findings(db, cond, now)]


def _title(c: Candidate) -> str:
    f = c.evidence
    return f"[POLICY-REVIEW] {f['policy_id']}: {LABELS[f['finding']].format(**f)}"


def _snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    return {"policy_id": c.evidence["policy_id"], "issue": None}


def _preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {"policy_id": c.evidence["policy_id"], "issue": {"title": _title(c), "assignee": autonomy.COMMITTEE_ASSIGNEE}}


def _apply(db: Session, actor: str, c: Candidate, pb: Any, now: datetime) -> dict[str, Any]:
    f = c.evidence
    issue = issues.open_issue(db, actor, asset=None, kind="policy_review", source_ref=f"policy-review:{f['policy_id']}:{f['finding']}",
                              level=EscalationLevel.COMMITTEE, assignee=autonomy.COMMITTEE_ASSIGNEE, title=_title(c),
                              description="Policy の見直しの候補です。Policy は変更していません。見直す場合は Policy 変更の提案と承認を経てください "
                                          "(§16 Policy。docs/adr/0001 区分 B)。")
    if issue is None:
        return {"after": {"policy_id": f["policy_id"], "issue": None}, "result": {"duplicate": True}}
    return {"after": {"policy_id": f["policy_id"], "issue": {"id": issue.id, "assignee": issue.assignee}}, "issue_id": issue.id}


autonomy.register_kind("policy_finding", detect_candidates)
autonomy.register_operation("propose.policy_review", Executor(_snapshot, _preview, _apply))
