"""§16 Webhook アダプタ: Outbox のイベントを配信する.

配信は at-least-once。受信側はイベント ID で冪等にする。再試行は指数バックオフで、最後まで届かなければ dead に移す。
製品に依存しないよう、送信 (WebhookTransport) と秘密鍵の解決 (SecretResolver) は Protocol で差し替えられる。
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.config import settings
from edgf.db import SessionLocal
from edgf.models import OutboxEvent, WebhookDelivery, WebhookEndpoint, utcnow
from edgf.services import audit
from edgf.services.outbox import to_cloudevent

log = logging.getLogger("edgf.webhooks")
PENDING, DELIVERED, DEAD = "pending", "delivered", "dead"
SIGNATURE_HEADER = "X-EDGF-Signature"
CONTENT_TYPE = "application/cloudevents+json"
SIGNATURE_TOLERANCE_SECONDS = 300
_SECRET_REF = re.compile(r"^env:[A-Z][A-Z0-9_]*$")
MIN_SECRET_BYTES = 32
BATCH_SIZE = 200
SYSTEM_ACTOR = "system:jobs"


class WebhookConfigError(ValueError):
    """配信先の設定の不備 (422)。"""


# ---------------------------------------------------------------- 署名
def sign(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, header: str, body: bytes, *, tolerance: int = SIGNATURE_TOLERANCE_SECONDS, now: float | None = None) -> bool:
    """受信側の検証の参照実装。時刻のずれが大きいものは再送の悪用を防ぐために拒否する。"""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        timestamp = int(parts["t"])
        expected = sign(secret, body, timestamp).split("v1=", 1)[1]
        signature = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------- 秘密鍵
class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str | None: ...


class EnvSecretResolver:
    """env:<環境変数名> を解決する。KMS などを使う場合は Protocol を実装して差し替える。"""

    def resolve(self, secret_ref: str) -> str | None:
        return os.environ.get(secret_ref.removeprefix("env:")) if _SECRET_REF.match(secret_ref) else None


# ---------------------------------------------------------------- 配信先の検証
def _is_public(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def check_destination(url: str) -> None:
    """配信先を検証する。本番では https のみとし、内部ネットワークへの配信を拒否する (SSRF 対策)。"""
    parts = urlsplit(url)
    production = settings.env == "production"
    if parts.scheme not in (("https",) if production else ("https", "http")) or not parts.hostname:
        raise WebhookConfigError("url は https (本番以外は http も可) で指定してください")
    if parts.username or parts.password:
        raise WebhookConfigError("url に認証情報を含めることはできません")
    if production and not _is_public(parts.hostname):
        raise WebhookConfigError("内部ネットワークの宛先には配信できません")


def check_secret(secret_ref: str, resolver: SecretResolver | None = None) -> None:
    if not _SECRET_REF.match(secret_ref):
        raise WebhookConfigError("secret_ref は env:<環境変数名> の形式で指定してください (鍵そのものは保存しません)")
    secret = (resolver or EnvSecretResolver()).resolve(secret_ref)
    if secret is None or len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise WebhookConfigError(f"secret_ref の鍵が見つからないか、{MIN_SECRET_BYTES} バイト未満です")


# ---------------------------------------------------------------- 送信
class WebhookTransport(Protocol):
    def send(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # リダイレクトで内部の宛先に誘導されないようにする
        return None


class UrllibTransport:
    """標準ライブラリだけの送信。リダイレクトは追わない (3xx は失敗として再試行する)。"""

    def send(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        check_destination(url)  # DNS の変更に備えて送信のたびに確認する
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - スキームは check_destination で検証済み
                return response.status
        except urllib.error.HTTPError as e:
            return e.code


# ---------------------------------------------------------------- 配信
def matches(patterns: Sequence[str], event_type: str) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(event_type, p) for p in patterns)


def fan_out(db: Session) -> int:
    """新しいイベントを、種別が一致する有効な配信先に割り当てる。"""
    events = db.scalars(select(OutboxEvent).where(OutboxEvent.fanned_out_at.is_(None)).order_by(OutboxEvent.created_at, OutboxEvent.id)).all()
    endpoints = db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.active.is_(True))).all()
    now = utcnow()
    created = 0
    for event in events:
        for endpoint in endpoints:
            if matches(endpoint.event_types, event.type):
                db.add(WebhookDelivery(event_id=event.id, endpoint_id=endpoint.id, status=PENDING, next_attempt_at=now))
                created += 1
        event.fanned_out_at = now
    db.flush()
    return created


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(settings.webhook_backoff_seconds * 2 ** (attempts - 1), settings.webhook_backoff_max_seconds))


def _attempt(delivery: WebhookDelivery, event: OutboxEvent, endpoint: WebhookEndpoint, transport: WebhookTransport,
             resolver: SecretResolver) -> str | None:
    """1 回送信する。成功なら None、失敗なら理由 (本文や秘密は含めない)。"""
    secret = resolver.resolve(endpoint.secret_ref)
    if not secret:
        return "secret_unavailable"  # 署名できないものは送らない
    body = json.dumps(to_cloudevent(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {"Content-Type": CONTENT_TYPE, SIGNATURE_HEADER: sign(secret, body, int(time.time())),
               "X-EDGF-Delivery": delivery.id, "User-Agent": "edgf-webhook"}
    try:
        status = transport.send(endpoint.url, body, headers, settings.webhook_timeout_seconds)
    except WebhookConfigError:
        return "destination_blocked"
    except Exception as e:  # noqa: BLE001 - 接続先の障害の種類を問わず再試行する
        return f"error:{type(e).__name__}"
    return None if 200 <= status < 300 else f"http:{status}"


def deliver_due(transport: WebhookTransport | None = None, resolver: SecretResolver | None = None) -> dict[str, int]:
    """期限の来た配信を処理する。送信の間は DB のトランザクションを持たず、1 件ごとに確定する。"""
    transport, resolver = transport or UrllibTransport(), resolver or EnvSecretResolver()
    with SessionLocal() as db:
        created = fan_out(db)
        db.commit()
        due = db.scalars(select(WebhookDelivery.id).join(WebhookEndpoint, WebhookDelivery.endpoint_id == WebhookEndpoint.id)
                         .where(WebhookDelivery.status == PENDING, WebhookDelivery.next_attempt_at <= utcnow(), WebhookEndpoint.active.is_(True))
                         .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id).limit(BATCH_SIZE)).all()
    counts = {"fanned_out": created, "delivered": 0, "retried": 0, "dead": 0}
    for delivery_id in due:
        with SessionLocal() as db:
            delivery = db.get(WebhookDelivery, delivery_id)
            event, endpoint = db.get(OutboxEvent, delivery.event_id), db.get(WebhookEndpoint, delivery.endpoint_id)
            db.rollback()  # 読み取りのトランザクションを閉じてから送信する
            error = _attempt(delivery, event, endpoint, transport, resolver)
            now = utcnow()
            delivery.attempts += 1
            delivery.last_error = error
            if error is None:
                delivery.status, delivery.delivered_at = DELIVERED, now
                counts["delivered"] += 1
            elif delivery.attempts >= settings.webhook_max_attempts:
                delivery.status, delivery.dead_at = DEAD, now
                audit.record(db, SYSTEM_ACTOR, "outbox.dead_letter", "webhook_delivery", delivery.id, {
                    "event_id": delivery.event_id, "endpoint_id": delivery.endpoint_id, "attempts": delivery.attempts, "last_error": error})
                counts["dead"] += 1
            else:
                delivery.next_attempt_at = now + _backoff(delivery.attempts)
                counts["retried"] += 1
            db.commit()
    return counts


def redeliver(db: Session, delivery: WebhookDelivery) -> None:
    if delivery.status != DEAD:
        raise ValueError(f"状態が {delivery.status} の配信は再送できません (デッドレターだけ)")
    delivery.status, delivery.attempts, delivery.next_attempt_at, delivery.dead_at = PENDING, 0, utcnow(), None
    db.flush()
