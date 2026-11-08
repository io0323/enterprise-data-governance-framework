"""§13 Evidence: 改ざん検知可能なハッシュチェーン監査ログ."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.errors import ProblemError
from edgf.models import AuditEvent, utcnow
from edgf.observability import request_id_var

GENESIS = "0" * 64

# 監査ログはハッシュチェーンのため消去できない。PII と生の識別子が入らないようにする (§12, §13)
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "identifier", "identifier_value", "raw_identifier", "subject_id", "subject_identifier", "data_subject_id",
    "email", "email_address", "mail", "phone", "phone_number", "address", "national_id", "ssn",
    "credit_card", "card_number",
})
FORBIDDEN_PAYLOAD_PATTERNS = (
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")),
    ("phone", re.compile(r"(?<![\w-])(?:\+\d{10,15}|0\d{1,4}-\d{1,4}-\d{4})(?![\w-])")),
    ("card_number", re.compile(r"(?<![\w-])\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?![\w-])")),
)
# 業務上の担当者 (データ主体ではない)。JWT の subject がメールアドレスの運用があるため値の検査から除く
OPERATOR_KEYS = frozenset({"owner", "steward", "custodian", "assignee", "subject", "granted_by", "requested_by",
                           "approved_by", "revoked_by", "recorded_by"})


class AuditPayloadError(ProblemError):
    """監査 payload に PII が含まれている。トランザクションごと拒否する (値は応答に含めない)。"""

    def __init__(self, path: str, kind: str) -> None:
        super().__init__(422, "個人情報の可能性がある値は記録できません。入力から取り除いてください (§12)",
                         type_="audit-payload-rejected", field=path, kind=kind)


def guard_payload(value: Any, path: str = "payload", operator: bool = False) -> None:
    """禁止キーと PII パターンを再帰的に検査する。"""
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k).lower()
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise AuditPayloadError(f"{path}.{k}", "forbidden_key")
            guard_payload(v, f"{path}.{k}", operator or key in OPERATOR_KEYS)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            guard_payload(v, f"{path}[{i}]", operator)
    elif isinstance(value, str) and not operator:
        for kind, pattern in FORBIDDEN_PAYLOAD_PATTERNS:
            if pattern.search(value):
                raise AuditPayloadError(path, kind)


def _digest(prev_hash: str, ts: str, actor: str, action: str, entity_type: str, entity_id: str, payload: Any,
            request_id: str | None = None) -> str:
    fields = {"prev": prev_hash, "ts": ts, "actor": actor, "action": action,
              "entity_type": entity_type, "entity_id": entity_id, "payload": payload}
    if request_id is not None:  # request_id 導入前のイベントのハッシュ互換を保つ
        fields["request_id"] = request_id
    body = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def record(db: Session, actor: str, action: str, entity_type: str, entity_id: str, payload: dict | None = None) -> AuditEvent:
    last = db.scalar(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1))
    prev = last.hash if last else GENESIS
    ts = utcnow()
    payload = json.loads(json.dumps(payload or {}, default=str))
    guard_payload(payload)
    rid = request_id_var.get()
    ev = AuditEvent(
        ts=ts, actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, payload=payload,
        request_id=rid, prev_hash=prev,
        hash=_digest(prev, ts.isoformat(), actor, action, entity_type, entity_id, payload, rid),
    )
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session) -> dict:
    prev = GENESIS
    count = 0
    for ev in db.scalars(select(AuditEvent).order_by(AuditEvent.seq)):
        expected = _digest(prev, ev.ts.isoformat(), ev.actor, ev.action, ev.entity_type, ev.entity_id, ev.payload,
                           ev.request_id)
        if ev.prev_hash != prev or ev.hash != expected:
            return {"valid": False, "events_checked": count, "broken_at_seq": ev.seq}
        prev = ev.hash
        count += 1
    return {"valid": True, "events_checked": count, "broken_at_seq": None}
