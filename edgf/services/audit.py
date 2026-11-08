"""§13 Evidence: 改ざん検知可能なハッシュチェーン監査ログ."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.models import AuditEvent, utcnow
from edgf.observability import request_id_var

GENESIS = "0" * 64


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
