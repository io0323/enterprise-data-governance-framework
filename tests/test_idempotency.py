"""Phase 15a: Idempotency-Key による送信の重複の防止 (サーバの側)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from edgf.db import SessionLocal, engine
from edgf.models import IdempotencyRecord, utcnow
from edgf.services import idempotency

API = "/api/v1"
ADMIN = {"X-Actor": "root", "X-Roles": "admin"}
OTHER = {"X-Actor": "other", "X-Roles": "admin"}


def _domain(client, name, key=None, headers=ADMIN):
    return client.post(f"{API}/domains", json={"name": name}, headers={**headers, **({"Idempotency-Key": key} if key else {})})


def _count(table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # nosec B608 - 固定の表名


def test_same_key_is_processed_once(client):
    first = _domain(client, "sales", "key-0000001")
    second = _domain(client, "sales", "key-0000001")
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json() and second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert _count("domains") == 1
    assert client.get(f"{API}/audit/events", headers=ADMIN, params={"action": "domain.create"}).headers["x-total-count"] == "1"


def test_without_key_duplicates_are_rejected_as_before(client):
    assert _domain(client, "sales").status_code == 201
    assert _domain(client, "sales").status_code == 409


def test_same_key_with_different_body_is_rejected(client):
    assert _domain(client, "sales", "key-0000002").status_code == 201
    r = _domain(client, "hr", "key-0000002")
    assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:idempotency-key-reused"
    assert _count("domains") == 1


def test_keys_are_scoped_per_actor(client):
    assert _domain(client, "sales", "key-0000003").status_code == 201
    r = _domain(client, "sales", "key-0000003", OTHER)
    assert r.status_code == 409 and "idempotent-replayed" not in r.headers  # 別の操作者では処理し直す (重複は既存の 409)


def test_failures_are_not_stored_and_can_be_retried(client):
    r = _domain(client, "sales", "key-0000004", {"X-Actor": "nobody", "X-Roles": ""})
    assert r.status_code == 403
    assert _domain(client, "sales", "key-0000004", {"X-Actor": "nobody", "X-Roles": "admin"}).status_code == 201


def test_invalid_key_is_problem(client):
    for bad in ("short", "x" * 201, "has space here", "日本語のキー12345"):
        r = client.post(f"{API}/domains", json={"name": "a"}, headers={**ADMIN, "Idempotency-Key": bad.encode("utf-8")})
        assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:idempotency-key-invalid"


def test_unauthenticated_request_is_left_to_the_endpoint(client):
    r = client.post(f"{API}/domains", json={"name": "a"}, headers={"X-Actor": "", "Idempotency-Key": "key-0000005"})
    assert r.status_code == 401 and _count("idempotency_records") == 0


def test_get_requests_ignore_the_header(client):
    assert client.get(f"{API}/domains", headers={**ADMIN, "Idempotency-Key": "key-0000006"}).status_code == 200
    assert _count("idempotency_records") == 0


def test_in_progress_and_expired_lock(client):
    fingerprint = idempotency.request_hash("POST", "/x", "", b"{}")
    assert idempotency.begin("a", "key-lock-1", fingerprint) is None
    with pytest.raises(idempotency.InProgress):
        idempotency.begin("a", "key-lock-1", fingerprint)
    with SessionLocal() as db:
        db.scalar(select(IdempotencyRecord)).locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert idempotency.begin("a", "key-lock-1", fingerprint) is None  # 異常終了した処理を引き継ぐ


def test_expired_records_are_purged(client):
    fingerprint = idempotency.request_hash("POST", "/x", "", b"{}")
    idempotency.begin("a", "key-exp-01", fingerprint)
    idempotency.finish("a", "key-exp-01", 201, "application/json", b"{}")
    with SessionLocal() as db:
        db.scalar(select(IdempotencyRecord)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert idempotency.begin("a", "key-exp-02", fingerprint) is None and _count("idempotency_records") == 1
    idempotency.finish("a", "missing-key", 201, None, b"")  # 記録が無くても例外にしない


def test_request_body_is_not_stored(client):
    marker = "zzbodymarker"
    assert client.post(f"{API}/domains", json={"name": "sales", "description": marker}, headers={**ADMIN, "Idempotency-Key": "key-0000007"}).status_code == 201
    with SessionLocal() as db:
        record = db.scalar(select(IdempotencyRecord))
        assert record.request_hash and len(record.request_hash) == 64


def test_body_limit_still_applies(client):
    from fastapi.testclient import TestClient

    from edgf.config import Settings
    from edgf.main import create_app

    with TestClient(create_app(Settings(auth_mode="dev", env="test", max_body_bytes=64)), headers=ADMIN) as small:
        r = small.post(f"{API}/domains", json={"name": "x" * 200}, headers={"Idempotency-Key": "key-0000008"})
        assert r.status_code == 413 and r.json()["type"] == "urn:edgf:problem:payload-too-large"


def test_request_hash_distinguishes_parts():
    base = idempotency.request_hash("POST", "/a", "", b"{}")
    assert base != idempotency.request_hash("POST", "/a", "x=1", b"{}")
    assert base != idempotency.request_hash("PUT", "/a", "", b"{}")
    assert idempotency.request_hash("POST", "/ab", "", b"") != idempotency.request_hash("POST", "/a", "", b"b")


def test_record_is_stored_before_the_response_is_returned(client):
    """応答を受け取った時点で記録が保存済み。すぐに再送しても「処理中」にならない。"""
    assert _domain(client, "sales", "key-0000009").status_code == 201
    with SessionLocal() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == "key-0000009"))
        assert record.status_code == 201 and record.response_body
    assert _domain(client, "sales", "key-0000009").headers["idempotent-replayed"] == "true"
