"""Idempotency-Key による送信の重複の防止.

Data Plane の SDK は送信系の要求に Idempotency-Key を付け、通信の失敗のときに同じ鍵で再送する。
同じ操作者が同じ鍵で送った要求は 1 回だけ処理し、2 回目以降は保存した応答を返す。

- 要求の本文は保存しない (ハッシュだけ)。応答は成功したもの (2xx) だけを有効期限まで保存する
- 鍵が同じで本文が違えば、鍵の使い回しとして拒否する
- 処理中の要求には短いロックを掛ける。異常終了してもロックが切れれば同じ鍵で処理し直せる
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.models import IdempotencyRecord, utcnow

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{8,200}$")
LOCK_SECONDS = 60


@dataclass(frozen=True)
class Stored:
    status_code: int
    content_type: str | None
    body: str


class KeyReused(Exception):
    """同じ鍵で、内容の違う要求が来た。"""


class InProgress(Exception):
    """同じ鍵の要求を処理している。"""


def request_hash(method: str, path: str, query: str, body: bytes) -> str:
    digest = hashlib.sha256()
    for part in (method.upper().encode(), path.encode(), query.encode(), body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def begin(actor: str, key: str, fingerprint: str) -> Stored | None:
    """処理を始めてよければ None を返す。処理済みなら保存した応答を返す。"""
    now = utcnow()
    with SessionLocal() as db:
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now))
        db.commit()
        existing = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor == actor, IdempotencyRecord.key == key))
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise KeyReused
            if existing.status_code is not None:
                return Stored(existing.status_code, existing.content_type, existing.response_body or "")
            if existing.locked_until > now:
                raise InProgress
            existing.locked_until = now + timedelta(seconds=LOCK_SECONDS)  # 異常終了した処理の引き継ぎ
            db.commit()
            return None
        db.add(IdempotencyRecord(actor=actor, key=key, request_hash=fingerprint, locked_until=now + timedelta(seconds=LOCK_SECONDS),
                                 expires_at=now + timedelta(hours=settings.idempotency_ttl_hours)))
        try:
            db.commit()
        except IntegrityError as e:  # 同時に同じ鍵が来た
            raise InProgress from e
    return None


def finish(actor: str, key: str, status_code: int, content_type: str | None, body: bytes) -> None:
    """成功した応答だけを保存する。失敗したら記録を消し、同じ鍵での再送を受け付ける。"""
    with SessionLocal() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor == actor, IdempotencyRecord.key == key))
        if record is None:
            return
        if 200 <= status_code < 300:
            record.status_code, record.content_type, record.response_body = status_code, content_type, body.decode("utf-8", errors="replace")
        else:
            db.delete(record)
        db.commit()
