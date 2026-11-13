"""§13 Evidence: 改ざん検知可能なハッシュチェーン監査ログ."""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from edgf.errors import ProblemError
from edgf.models import AuditEvent, fit_identifier, utcnow
from edgf.observability import channel_var, request_id_var

GENESIS = "0" * 64
MAX_ENTITY_ID_LENGTH = 300
# チェーンの先頭の取り合いで再試行する回数。書き込みは 1 件ずつしか進まないので、同時実行数より十分大きくする
MAX_WRITE_ATTEMPTS = 100
CHAIN_LOCK_KEY = 7_046_209_001  # 監査チェーンの advisory lock のキー (任意の固定値)

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


class AuditChainContention(ProblemError):
    """再試行しても監査チェーンに書き込めなかった。リクエストごと失敗させる (証跡なしでは確定しない)。"""

    def __init__(self) -> None:
        super().__init__(503, "監査ログへの書き込みが混み合っています。時間をおいて再試行してください",
                         type_="audit-chain-contention", headers={"Retry-After": "1"})


def _latest_hash(db: Session) -> str:
    return db.scalar(select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)) or GENESIS


def _is_chain_conflict(exc: IntegrityError | OperationalError) -> bool:
    """セーブポイントからの再試行で回復できる失敗か。

    監査チェーン以外の一意制約違反は呼び出し元に返す。デッドロックも再試行しない: セーブポイントまで戻っても
    それ以前に取った行ロックは解放されないので、相手が進めずライブロックになる。リクエストごと失敗させて
    ロックを解放する (edgf.errors で 503)。
    """
    message = str(getattr(exc, "orig", exc))
    if isinstance(exc, IntegrityError):
        return "audit_events" in message
    return "database is locked" in message  # SQLite の書き込みロック


def _backoff(attempt: int) -> None:
    time.sleep(random.uniform(0, min(0.002 * attempt, 0.05)))  # nosec B311 - 再試行の間隔をずらすだけで、暗号用途ではない


def record(db: Session, actor: str, action: str, entity_type: str, entity_id: str, payload: dict | None = None) -> AuditEvent:
    """監査イベントをハッシュチェーンに追記する。

    並行する書き込みは同じ prev_hash を読むことがあるが、UNIQUE 制約で 1 件だけが成功するので分岐しない。
    失敗した側はセーブポイントまで戻して先頭を読み直す (リクエストの他の変更は失われない)。
    勝った側のトランザクションが確定するまで UNIQUE 検査で待つので、書き込みはチェーン上で直列化される。
    先頭を読み直せる必要があるので、分離レベルは READ COMMITTED が前提。

    PostgreSQL では先に advisory lock を取り、トランザクションの終わりまで順番に書く。衝突して再試行する無駄が無くなる。
    UNIQUE 制約と再試行は、ロックを取らない経路 (SQLite や手動の操作) のために残す。
    """
    payload = json.loads(json.dumps(payload or {}, default=str))
    channel = channel_var.get()
    if channel:  # UI のセッションの経路の操作 (Phase 17)。セッション ID はハッシュの先頭だけ
        payload = {**payload, **channel}
    guard_payload(payload)
    entity_id = fit_identifier(entity_id, MAX_ENTITY_ID_LENGTH)
    rid = request_id_var.get()
    db.flush()  # 他の変更のエラーをチェーンの衝突と混同しないよう、先に確定させる
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(CHAIN_LOCK_KEY)))
    for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
        prev = _latest_hash(db)
        ts = utcnow()
        ev = AuditEvent(
            ts=ts, actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, payload=payload,
            request_id=rid, prev_hash=prev,
            hash=_digest(prev, ts.isoformat(), actor, action, entity_type, entity_id, payload, rid),
        )
        try:
            with db.begin_nested():
                db.add(ev)
                db.flush()
            return ev
        except (IntegrityError, OperationalError) as e:
            if not _is_chain_conflict(e):
                raise
            _backoff(attempt)
    raise AuditChainContention()


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
