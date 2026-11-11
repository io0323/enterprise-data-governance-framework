"""§7 Discovery / §6.3 Metadata First: Data Plane の資産一覧の取り込みと差分の検出.

Data Plane はソース (namespace) ごとに全量のスナップショットを送る。Control Plane は同じソースの前回の
スナップショットと比べ、次のものを検出する。送られてこなかったソースの資産には触れない (送り漏れを消失と誤判定しない)。

- 未登録: Catalog に無い資産。対応表のドメインの Steward に Issue を起票する (重複起票はしない)
- 消失: 前回あったのに今回無い資産。Catalog にあれば、その資産の Steward に Issue を起票する
- スキーマの変化: Catalog のスキーマと比べ、Phase 11 の互換性の判定表で非破壊なら自動で反映する。
  破壊的なら保留にして Issue を起票し、Steward / Owner の確認のあとで反映する

受け取るのはメタデータ (名前、種類、カラムの名前・型・nullable、更新時刻) だけ。値、サンプル、統計は入力の検証で拒否する。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.enums import IssueStatus
from edgf.models import DataAsset, DiscoveredAsset, DiscoverySnapshot, DiscoverySource, Issue, SchemaChangeProposal, to_naive_utc, utcnow
from edgf.services import asset_update, audit, issues
from edgf.services.discovery_rules import SourceRule
from edgf.services.mesh_compat import compare_schema

SYSTEM_ACTOR = "system:discovery"
ISSUE_KIND = "discovery"


def _column(c: dict[str, Any]) -> dict[str, Any]:
    out = {"name": c["name"], "type": c["type"]}
    if c.get("nullable") is not None:
        out["nullable"] = c["nullable"]
    return out


def merged_schema(catalog: Sequence[dict[str, Any]], discovered: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Data Plane の構造 (名前、型、nullable) に、Catalog の業務のメタデータ (説明、PII など) を残して重ねる。"""
    existing = {c["name"]: c for c in catalog}
    out = []
    for col in discovered:
        base = dict(existing.get(col["name"], {}))
        base.update(_column(col))
        out.append(base)
    return out


def _catalog_asset(db: Session, qualified_name: str) -> DataAsset | None:
    return db.scalar(select(DataAsset).where(DataAsset.qualified_name == qualified_name, DataAsset.lifecycle_state != "deleted"))


def _reconcile_schema(db: Session, actor: str, asset: DataAsset, namespace: str, discovered: list[dict[str, Any]]) -> str | None:
    """'applied'、'pending'、変化なしは None。"""
    proposed = merged_schema(asset.schema_ or [], discovered)
    changes = compare_schema(asset.schema_ or [], proposed)
    pending = db.scalars(select(SchemaChangeProposal).where(SchemaChangeProposal.asset_id == asset.id, SchemaChangeProposal.status == "pending")).all()
    if not changes:
        for row in pending:  # Data Plane の構造が Catalog と同じに戻った
            row.status = "superseded"
        return None
    if not any(c["breaking"] for c in changes):
        for row in pending:  # 自動で反映できる状態になったので、保留の案は不要
            row.status = "superseded"
        asset_update.apply(db, asset, {"schema_": proposed}, SYSTEM_ACTOR)
        return "applied"
    if pending and pending[-1].proposed_schema == proposed:
        return "pending"  # 同じ案は作り直さない
    for row in pending:
        row.status = "superseded"
    issue = issues.open_issue(db, SYSTEM_ACTOR, asset=asset, kind=ISSUE_KIND, title=f"スキーマの破壊的な変化: {asset.qualified_name}",
                              description="Data Plane の構造が Catalog と互換でない形で変わりました。確認のうえ反映してください (§7 Discovery)",
                              severity="high", source_ref="discovery-breaking")
    existing = db.scalar(select(Issue).where(Issue.asset_id == asset.id, Issue.source_ref == "discovery-breaking"))
    db.add(SchemaChangeProposal(asset_id=asset.id, namespace=namespace, proposed_schema=proposed, changes=changes,
                                issue_id=(issue or existing).id if (issue or existing) else None))
    db.flush()
    audit.record(db, actor, "discovery.schema_pending", "asset", asset.id, {"changes": [c["code"] for c in changes]})
    return "pending"


def ingest(db: Session, actor: str, rule: SourceRule, namespace: str, assets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    now = utcnow()
    source = db.get(DiscoverySource, namespace)
    if source is None:
        source = DiscoverySource(namespace=namespace, domain=rule.domain, steward=rule.steward)
        db.add(source)
    source.domain, source.steward = rule.domain, rule.steward
    previous = {row.qualified_name: row for row in db.scalars(select(DiscoveredAsset).where(DiscoveredAsset.namespace == namespace))}
    incoming = {a["qualified_name"]: a for a in assets}
    added, changed, missing = [], [], []
    for name, item in incoming.items():
        schema = [_column(c) for c in item.get("schema") or []]
        updated = to_naive_utc(item["updated_at"]) if item.get("updated_at") else None
        row = previous.get(name)
        if row is None:
            db.add(DiscoveredAsset(namespace=namespace, qualified_name=name, kind=item.get("kind"), schema_=schema, source_updated_at=updated,
                                   first_seen_at=now, last_seen_at=now))
            added.append(name)
            continue
        if row.status != "present":
            added.append(name)  # 消失していた資産が戻った
        elif row.schema_ != schema:
            changed.append(name)
        row.status, row.missing_since, row.last_seen_at = "present", None, now
        row.kind, row.schema_, row.source_updated_at = item.get("kind"), schema, updated
    for name, row in previous.items():
        if name not in incoming and row.status == "present":
            row.status, row.missing_since = "missing", now
            missing.append(name)
    db.flush()

    unregistered, applied, pending = [], [], []
    for name, item in incoming.items():
        asset = _catalog_asset(db, name)
        if asset is None:
            unregistered.append(name)
            issues.open_issue(db, SYSTEM_ACTOR, asset=None, kind=ISSUE_KIND, title=f"未登録の資産: {name}",
                              description=f"Data Plane のソース {namespace} にあり、Catalog に登録されていません (§7 Discovery、§6.3 Metadata First)",
                              source_ref=f"discovery-unregistered:{name}", assignee=rule.steward)
            continue
        if item.get("schema"):
            result = _reconcile_schema(db, actor, asset, namespace, [_column(c) for c in item["schema"]])
            if result == "applied":
                applied.append(name)
            elif result == "pending":
                pending.append(name)
    for name in missing:
        asset = _catalog_asset(db, name)
        if asset is not None:
            issues.open_issue(db, SYSTEM_ACTOR, asset=asset, kind=ISSUE_KIND, title=f"Data Plane で見つかりません: {name}",
                              description=f"ソース {namespace} の資産一覧から消えました。廃止か移動かを確認してください (§7 Discovery)",
                              source_ref=f"discovery-missing:{namespace}")
    snapshot = DiscoverySnapshot(namespace=namespace, received_at=now, actor=actor, asset_count=len(incoming), added_count=len(added),
                                 missing_count=len(missing), changed_count=len(changed))
    db.add(snapshot)
    db.flush()
    source.last_snapshot_id, source.last_snapshot_at = snapshot.id, now
    audit.record(db, actor, "discovery.snapshot", "discovery_source", namespace, {
        "snapshot_id": snapshot.id, "assets": len(incoming), "added": len(added), "missing": len(missing), "changed": len(changed),
        "unregistered": len(unregistered), "schema_applied": len(applied), "schema_pending": len(pending)})
    return {"snapshot_id": snapshot.id, "namespace": namespace, "domain": rule.domain, "asset_count": len(incoming), "added": sorted(added),
            "missing": sorted(missing), "changed": sorted(changed), "unregistered": sorted(unregistered), "schema_applied": sorted(applied),
            "schema_pending": sorted(pending)}


def apply_proposal(db: Session, actor: str, proposal: SchemaChangeProposal, asset: DataAsset) -> dict[str, Any]:
    """保留していた破壊的な変化を、確認した利用者として反映する (既存の更新の経路を通す)。"""
    changes = asset_update.apply(db, asset, {"schema_": proposal.proposed_schema}, actor)
    proposal.status, proposal.decided_by, proposal.decided_at = "applied", actor, utcnow()
    issue = db.get(Issue, proposal.issue_id) if proposal.issue_id else None
    if issue is not None and issue.status in issues.OPEN_STATES:
        issue.status, issue.resolved_at = IssueStatus.RESOLVED.value, utcnow()
    db.flush()
    audit.record(db, actor, "discovery.schema_applied", "asset", asset.id, {"proposal_id": proposal.id, "changes": [c["code"] for c in proposal.changes]})
    return changes


def registration(db: Session, domain: str | None = None) -> dict[str, Any]:
    """Catalog 登録率 = 登録済み ÷ Data Plane で検出された資産 (§1.8 の目標 95%)。検出が無ければ None。"""
    stmt = select(DiscoveredAsset.qualified_name).where(DiscoveredAsset.status == "present")
    if domain:
        stmt = stmt.join(DiscoverySource, DiscoverySource.namespace == DiscoveredAsset.namespace).where(DiscoverySource.domain == domain)
    discovered = set(db.scalars(stmt))
    registered = set(db.scalars(select(DataAsset.qualified_name).where(DataAsset.qualified_name.in_(discovered),
                                                                       DataAsset.lifecycle_state != "deleted"))) if discovered else set()
    total = len(discovered)
    return {"discovered_asset_count": total, "unregistered_asset_count": total - len(registered),
            "catalog_registration_rate": round(len(registered) / total, 4) if total else None}


def source_summary(db: Session, namespaces: Sequence[str]) -> dict[str, dict[str, int]]:
    rows = db.execute(select(DiscoveredAsset.namespace, DiscoveredAsset.status, func.count()).where(DiscoveredAsset.namespace.in_(namespaces))
                      .group_by(DiscoveredAsset.namespace, DiscoveredAsset.status)).all()
    out: dict[str, dict[str, int]] = {n: {"present": 0, "missing": 0} for n in namespaces}
    for namespace, status, count in rows:
        out[namespace][status] = count
    return out
