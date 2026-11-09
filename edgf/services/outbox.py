"""§16 Transactional Outbox: イベントは状態変更と同じトランザクションで書き込む.

形式は CloudEvents 1.0。payload には監査と同じガードを適用する (PII と生の識別子を外へ出さない。§12)。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from edgf.models import OutboxEvent, fit_identifier
from edgf.observability import request_id_var
from edgf.services.audit import guard_payload

SOURCE = "urn:edgf:control-plane"
TYPE_PREFIX = "edgf."
MAX_SUBJECT_LENGTH = 300


def emit(db: Session, event_type: str, subject: str, data: Mapping[str, Any]) -> OutboxEvent:
    """イベントを書き込む。呼び出し元のトランザクションが巻き戻れば、イベントも残らない。"""
    guard_payload(data, "event")
    event = OutboxEvent(type=f"{TYPE_PREFIX}{event_type}", subject=fit_identifier(subject, MAX_SUBJECT_LENGTH),
                        data=json.loads(json.dumps(dict(data), default=str, ensure_ascii=False)), request_id=request_id_var.get())
    db.add(event)
    db.flush()
    return event


def _rfc3339(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def to_cloudevent(event: OutboxEvent) -> dict[str, Any]:
    """CloudEvents 1.0 の structured mode の表現。"""
    envelope: dict[str, Any] = {"specversion": "1.0", "id": event.id, "source": SOURCE, "type": event.type, "subject": event.subject,
                                "time": _rfc3339(event.created_at), "datacontenttype": "application/json", "data": event.data}
    if event.request_id:
        envelope["edgfrequestid"] = event.request_id
    return envelope
