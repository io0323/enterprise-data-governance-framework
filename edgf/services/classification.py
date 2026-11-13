"""§3 Data Classification / §12 PII / §20 Level 5: メタデータからの分類の提案と確定 (Phase 19。docs/adr/0001).

根拠はメタデータだけ:
- カラム名と説明文の、日本語・英語の辞書規則 (policies/classification/rules.yaml。Policy Bundle に含める)
- Lineage の DIRECT の経路 (Phase 16 のカラム単位のリネージ。マスキングを伴う経路は使わない)
- 資産に紐付いた Glossary の用語 (カラム名が用語の名前か同義語に当たるとき、用語の名前・同義語・定義に辞書規則を当てる)
ML のモデルや外部の LLM は使わない。データの値も見ない (品質検査の本文の値は Control Plane に保存されない)。

提案は保護を強める方向 (pii、pii_category、sensitivity を上げる、cde) だけで、現在の値と違う項目だけを含む。
確定は対象資産の Owner / Steward が承認エンジンの案件として行い (区分 B。✓)、既存の更新の経路 (asset_update.apply) を通す。
却下された提案は、根拠の内容 (ハッシュで比べる) が変わらない限り、二度と出さない。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.auth import SENSITIVITY_LEVELS
from edgf.enums import LifecycleState
from edgf.models import (ApprovalRequest, ClassificationSuggestion, ColumnLineageEdge, DataAsset, GlossaryTerm, new_id, term_asset,
                         utcnow)
from edgf.services import approvals, asset_update, audit, autonomy, outbox
from edgf.services.autonomy import Candidate, Executor
from edgf.services.classification_rules import Rules, is_ascii, active, normalize, tokens

PENDING, PROPOSED, ACCEPTED, REJECTED, SUPERSEDED, CANCELLED = "pending", "proposed", "accepted", "rejected", "superseded", "cancelled"
OPEN_STATUSES = (PENDING, PROPOSED)
DESCRIPTION_FACTOR = 0.8   # 説明文だけで一致したときの確度の係数
GLOSSARY_FACTOR = 0.9      # Glossary の用語を通して一致したときの係数
LINEAGE_IDENTITY, LINEAGE_OTHER = 0.85, 0.75
# ---------------------------------------------------------------- 根拠の収集 (メタデータだけ)
def _level(sensitivity: str | None) -> int:
    return SENSITIVITY_LEVELS.index(sensitivity) if sensitivity in SENSITIVITY_LEVELS else -1


def _rule_evidence(rules: Rules, column: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rule in rules.items:
        hit = rule.matches(column.get("name", ""))
        field, factor = "name", 1.0
        if hit is None:
            hit, field, factor = rule.matches(column.get("description", "")), "description", DESCRIPTION_FACTOR
        if hit is not None:
            out.append({"source": "rule", "rule_id": rule.id, "field": field, "pattern": hit, "suggest": dict(rule.suggest),
                        "confidence": round(rule.confidence * factor, 4)})
    return out


def _lineage_evidence(db: Session, asset: DataAsset, column: str, upstream: dict[str, DataAsset | None]) -> list[dict[str, Any]]:
    edges = db.scalars(select(ColumnLineageEdge).where(ColumnLineageEdge.target == asset.qualified_name, ColumnLineageEdge.target_column == column,
                                                       ColumnLineageEdge.type == "DIRECT", ColumnLineageEdge.masking.is_(False))
                       .order_by(ColumnLineageEdge.source, ColumnLineageEdge.source_column)).all()
    out = []
    for edge in edges:
        if edge.source not in upstream:
            upstream[edge.source] = db.scalar(select(DataAsset).where(DataAsset.qualified_name == edge.source))
        source = upstream[edge.source]
        if source is None:
            continue
        spec = next((c for c in source.schema_ or [] if c.get("name") == edge.source_column), None)
        suggest: dict[str, Any] = {}
        if spec and spec.get("pii"):
            suggest["pii"] = True
            if spec.get("pii_category"):
                suggest["pii_category"] = spec["pii_category"]
        if _level(source.sensitivity) > _level(asset.sensitivity):
            suggest["sensitivity"] = source.sensitivity
        if suggest:
            out.append({"source": "lineage", "upstream": edge.source, "upstream_column": edge.source_column, "subtype": edge.subtype,
                        "suggest": suggest, "confidence": LINEAGE_IDENTITY if (edge.subtype or "").upper() == "IDENTITY" else LINEAGE_OTHER})
    return out


def _term_names(term: GlossaryTerm) -> list[str]:
    return [term.name, *[s for s in term.synonyms or [] if isinstance(s, str)]]


def _glossary_evidence(rules: Rules, terms: Sequence[GlossaryTerm], column: Mapping[str, Any]) -> list[dict[str, Any]]:
    name_tokens, name = tokens(column.get("name", "")), normalize(column.get("name", "")).lower()
    out = []
    for term in terms:
        names = _term_names(term)
        linked = any((tokens(n) and tokens(n) == name_tokens) or (not is_ascii(n) and normalize(n).lower() == name) for n in names)
        if not linked:
            continue
        text = " / ".join([*names, term.definition or ""])
        for rule in rules.items:
            hit = rule.matches(text)
            if hit is not None:
                out.append({"source": "glossary", "term": term.name, "rule_id": rule.id, "pattern": hit, "suggest": dict(rule.suggest),
                            "confidence": round(rule.confidence * GLOSSARY_FACTOR, 4)})
    return out


def _merge(evidence: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], float]:
    proposal: dict[str, Any] = {}
    best_category = -1.0
    for e in sorted(evidence, key=lambda x: -x["confidence"]):
        s = e["suggest"]
        if s.get("pii"):
            proposal["pii"] = True
        if s.get("cde"):
            proposal["cde"] = True
        if "pii_category" in s and e["confidence"] > best_category:
            proposal["pii_category"], best_category = s["pii_category"], e["confidence"]
        if "sensitivity" in s and _level(s["sensitivity"]) > _level(proposal.get("sensitivity")):
            proposal["sensitivity"] = s["sensitivity"]
    remaining = 1.0
    for e in evidence:
        remaining *= 1 - e["confidence"]
    return proposal, round(min(1 - remaining, 0.99), 4)


def _raising(proposal: Mapping[str, Any], spec: Mapping[str, Any], asset: DataAsset) -> tuple[dict[str, Any], dict[str, Any]]:
    """現在より保護を強める項目だけを残す。(提案, 現在の値)。人が決めた pii_category は上書きしない。"""
    out, current = {}, {}
    if proposal.get("pii") and not spec.get("pii"):
        out["pii"], current["pii"] = True, bool(spec.get("pii"))
    if proposal.get("pii_category") and not spec.get("pii_category"):
        out["pii_category"], current["pii_category"] = proposal["pii_category"], spec.get("pii_category")
    if proposal.get("cde") and not spec.get("cde"):
        out["cde"], current["cde"] = True, bool(spec.get("cde"))
    if proposal.get("sensitivity") and _level(proposal["sensitivity"]) > _level(asset.sensitivity):
        out["sensitivity"], current["sensitivity"] = proposal["sensitivity"], asset.sensitivity
    return out, current


def evidence_hash(column: str, evidence: Sequence[Mapping[str, Any]], proposal: Mapping[str, Any]) -> str:
    body = json.dumps({"column": column, "evidence": sorted(evidence, key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False)),
                       "proposal": proposal}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def suggest_for(db: Session, asset: DataAsset, rules: Rules | None = None) -> list[dict[str, Any]]:
    """1 つの資産の提案 (保存しない)。入力は資産のメタデータ、カラム単位のリネージ、Glossary の用語だけ。"""
    rules = rules if rules is not None else active(db)
    terms = db.scalars(select(GlossaryTerm).join(term_asset, term_asset.c.term_id == GlossaryTerm.id)
                       .where(term_asset.c.asset_id == asset.id).order_by(GlossaryTerm.name)).all()
    upstream: dict[str, DataAsset | None] = {}
    out = []
    for spec in asset.schema_ or []:
        name = spec.get("name")
        if not name:
            continue
        evidence = [*_rule_evidence(rules, spec), *_lineage_evidence(db, asset, name, upstream), *_glossary_evidence(rules, terms, spec)]
        if not evidence:
            continue
        proposal, confidence = _merge(evidence)
        raising, current = _raising(proposal, spec, asset)
        if raising:
            out.append({"column": name, "proposal": raising, "current": current, "evidence": evidence, "confidence": confidence,
                        "evidence_hash": evidence_hash(name, evidence, raising)})
    return out


# ---------------------------------------------------------------- 分析 (提案の作成)
def scan(db: Session, now: datetime) -> dict[str, Any]:
    """全資産の提案を作る。同じ根拠の提案は作り直さない (却下されたものも)。根拠が変われば、古い未決の提案は superseded にする。"""
    rules = active(db)
    created = superseded = 0
    assets = db.scalars(select(DataAsset).where(DataAsset.lifecycle_state != LifecycleState.DELETED.value).order_by(DataAsset.qualified_name)).all()
    for asset in assets:
        for s in suggest_for(db, asset, rules):
            known = db.scalar(select(ClassificationSuggestion.id).where(
                ClassificationSuggestion.asset_id == asset.id, ClassificationSuggestion.column == s["column"],
                ClassificationSuggestion.evidence_hash == s["evidence_hash"]).limit(1))
            if known:
                continue
            for old in db.scalars(select(ClassificationSuggestion).where(
                    ClassificationSuggestion.asset_id == asset.id, ClassificationSuggestion.column == s["column"],
                    ClassificationSuggestion.status == PENDING)):
                old.status = SUPERSEDED
                superseded += 1
            row = ClassificationSuggestion(id=new_id(), asset_id=asset.id, column=s["column"], proposal=s["proposal"], current=s["current"],
                                           evidence=s["evidence"], confidence=s["confidence"], evidence_hash=s["evidence_hash"],
                                           status=PENDING, created_at=now, decision_reason="")
            db.add(row)
            db.flush()
            outbox.emit(db, "classification.suggested", row.id, {"suggestion_id": row.id, "asset_id": asset.id, "column": row.column})
            created += 1
    return {"assets": len(assets), "created": created, "superseded": superseded}


def view(row: ClassificationSuggestion, asset: DataAsset | None = None, approval: ApprovalRequest | None = None) -> dict[str, Any]:
    return {"id": row.id, "asset_id": row.asset_id, "qualified_name": asset.qualified_name if asset else None, "column": row.column,
            "proposal": row.proposal, "current": row.current, "evidence": row.evidence, "confidence": row.confidence,
            "evidence_hash": row.evidence_hash, "status": row.status, "approval_id": row.approval_id,
            "approval_status": approval.status if approval else None, "created_at": row.created_at,
            "decided_by": row.decided_by, "decided_at": row.decided_at, "decision_reason": row.decision_reason}


# ---------------------------------------------------------------- プレイブックの対象と、区分 B の操作 (提案を承認エンジンに作る)
def _candidate(db: Session, row: ClassificationSuggestion, cond: Mapping[str, Any]) -> Candidate | None:
    if row.status != PENDING or row.confidence < cond["min_confidence"]:
        return None
    return Candidate("classification_suggestion", row.id, row.id, db.get(DataAsset, row.asset_id),
                     {"column": row.column, "proposal": row.proposal, "confidence": row.confidence, "evidence": row.evidence})


def detect_candidates(db: Session, cond: Mapping[str, Any], now: datetime) -> list[Candidate]:
    rows = db.scalars(select(ClassificationSuggestion).where(ClassificationSuggestion.status == PENDING)
                      .order_by(ClassificationSuggestion.confidence.desc(), ClassificationSuggestion.created_at, ClassificationSuggestion.id)).all()
    return [c for c in (_candidate(db, r, cond) for r in rows) if c is not None]


def load_candidate(db: Session, target_id: str, cond: Mapping[str, Any], now: datetime) -> Candidate | None:
    row = db.get(ClassificationSuggestion, target_id)
    return _candidate(db, row, cond) if row else None


def _snapshot(db: Session, c: Candidate) -> dict[str, Any]:
    return dict(db.get(ClassificationSuggestion, c.target_id).current)


def _preview(db: Session, c: Candidate, actor: str, now: datetime) -> dict[str, Any]:
    return {**_snapshot(db, c), "proposal": c.evidence["proposal"]}  # 確定されるまで、資産の分類は変わらない


def _apply(db: Session, actor: str, c: Candidate, pb: Any, now: datetime) -> dict[str, Any]:
    row = db.get(ClassificationSuggestion, c.target_id, with_for_update=True)
    req = approvals.create(db, actor, type_name="classification_suggestion", requested_by=actor, target_type="classification_suggestion",
                           target_id=row.id, asset=c.asset, reason=f"分類の提案: {c.asset.qualified_name}.{row.column} (確度 {row.confidence})",
                           payload={"column": row.column, "proposal": row.proposal, "confidence": row.confidence})
    row.status, row.approval_id = PROPOSED, req.id
    db.flush()
    return {"after": _preview(db, c, actor, now), "approval_id": req.id}


# ---------------------------------------------------------------- 確定と却下 (承認エンジンの処理)
def get(db: Session, suggestion_id: str, *, lock: bool = False) -> ClassificationSuggestion | None:
    return db.get(ClassificationSuggestion, suggestion_id, with_for_update=lock)


def changes_for(asset: DataAsset, row: ClassificationSuggestion) -> dict[str, Any]:
    """確定する変更 (資産の更新の経路に渡す形)。対象のカラムが無くなっていれば ApprovalConflict。"""
    schema = [dict(c) for c in asset.schema_ or []]
    spec = next((c for c in schema if c.get("name") == row.column), None)
    if spec is None:
        raise approvals.ApprovalConflict(f"カラム {row.column} は資産に存在しません (提案の後にスキーマが変わりました)")
    changes: dict[str, Any] = {}
    column_changed = False
    for key in ("pii", "pii_category", "cde"):
        if key in row.proposal and not spec.get(key):
            spec[key] = row.proposal[key]
            column_changed = True
    if column_changed:
        changes["schema_"] = schema
    if "sensitivity" in row.proposal and _level(row.proposal["sensitivity"]) > _level(asset.sensitivity):
        changes["sensitivity"] = row.proposal["sensitivity"]
    return changes


def on_approved(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    """確定: 既存の更新の経路 (PATCH /assets と同じ asset_update.apply) を通す。Certification のリセットなども同じく発火する。"""
    row = get(db, req.target_id, lock=True)
    asset = db.get(DataAsset, req.asset_id, with_for_update=True)
    changes = changes_for(asset, row)
    applied = sorted(changes)
    if changes:
        asset_update.apply(db, asset, changes, actor)
    row.status, row.decided_by, row.decided_at = ACCEPTED, decided_by, utcnow()
    db.flush()
    audit.record(db, actor, "classification.accept", "classification_suggestion", row.id, {
        "asset_id": asset.id, "column": row.column, "proposal": row.proposal, "applied": applied, "approval_id": req.id})


def on_rejected(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    row = get(db, req.target_id, lock=True)
    row.status, row.decided_by, row.decided_at = REJECTED, decided_by, utcnow()
    db.flush()
    audit.record(db, actor, "classification.reject", "classification_suggestion", row.id, {
        "asset_id": row.asset_id, "column": row.column, "evidence_hash": row.evidence_hash, "approval_id": req.id})


def on_cancelled(db: Session, actor: str, req: ApprovalRequest, decided_by: str) -> None:
    row = get(db, req.target_id, lock=True)
    row.status = CANCELLED
    db.flush()


def decide(db: Session, principal: Any, row: ClassificationSuggestion, *, accept: bool, reason: str) -> ClassificationSuggestion:
    """確定・却下。判断者の判定 (対象資産の Owner / Steward、職務分離、ステップアップ) は承認エンジンが行う。"""
    if row.status != PROPOSED or not row.approval_id:
        raise approvals.ApprovalConflict(f"承認の案件になっていない提案は判断できません (状態: {row.status})")
    req = db.get(ApprovalRequest, row.approval_id, with_for_update=True)
    row.decision_reason = reason
    approvals.decide(db, principal, req, approve=accept, comment=reason)
    return row


def inbox_operations(db: Session, req: ApprovalRequest) -> tuple[str, tuple[dict[str, Any], ...]]:
    from edgf.services.approval_sources import field_, operation  # 循環 import 回避

    row = get(db, req.target_id)
    asset = db.get(DataAsset, row.asset_id)
    path = f"/api/v1/autonomy/suggestions/{row.id}/decision"
    changes = ", ".join(f"{k}={v}" for k, v in sorted(row.proposal.items()))
    return (f"分類の提案: {asset.qualified_name}.{row.column} ({changes}。確度 {row.confidence})",
            (operation("adopt", "POST", path, "classification.suggestion.decide", body={"accept": True}, fields=[field_("reason")]),
             operation("reject", "POST", path, "classification.suggestion.decide", body={"accept": False},
                       fields=[field_("reason", required=True)])))


autonomy.register_analysis("classification_scan", scan)
autonomy.register_kind("classification_suggestion", detect_candidates, load_candidate)
autonomy.register_operation("propose.classification", Executor(_snapshot, _preview, _apply,
                                                              applicable=lambda db, c: db.get(ClassificationSuggestion, c.target_id).status == PENDING))
