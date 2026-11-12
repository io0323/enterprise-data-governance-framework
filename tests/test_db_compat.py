"""PostgreSQL / SQLite の差で問題になる点の検証 (両方の DB で実行する)。

文字列長の超過、タイムゾーン、JSON、ilike、一意制約違反の例外、トランザクション、監査チェーンの同時書き込み。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from edgf.db import SessionLocal, engine, get_session
from edgf.main import create_app
from edgf.models import AuditEvent, Domain, MdmXref
from edgf.services import audit
from tests.conftest import H
from tests.mdm_helpers import HUB, MDM, master_asset

pytestmark = pytest.mark.fresh_schema  # 並行実行、コミットとロールバック、監査チェーンの競合、DDL を扱う (テストごとにスキーマを作り直す)

API = "/api/v1"
ADMIN = {"X-Actor": "root", "X-Roles": "admin", "X-Domains": "*"}
COMMITTEE = {"X-Actor": "chair", "X-Roles": "committee", "X-Domains": ""}
is_postgres = engine.dialect.name == "postgresql"
needs_real_concurrency = pytest.mark.skipif(not is_postgres, reason="インメモリの SQLite は 1 接続を共有するため並行実行できない")


def _asset(client, qn="s.a", **kw):
    r = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": qn, "name": qn, "data_class": "operational",
                                                          "sensitivity": "internal", "owner": "o", "steward": "st", **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 文字列長の超過
@pytest.mark.parametrize("path, body", [
    ("/domains", {"name": "d", "owner": "x" * 201}),
    ("/assets", {"qualified_name": "a.b", "name": "x" * 201, "data_class": "master", "sensitivity": "internal"}),
    ("/assets", {"qualified_name": "a.b", "name": "n", "owner": "x" * 201, "data_class": "master", "sensitivity": "internal"}),
    ("/glossary/terms", {"name": "x" * 201, "definition": "d"}),
    ("/glossary/terms", {"name": "t", "definition": "d", "steward": "x" * 201}),
    ("/issues", {"title": "x" * 301}),
    ("/issues", {"title": "t", "kind": "x" * 21}),
    ("/issues", {"title": "t", "assignee": "x" * 201}),
    ("/lineage/edges", {"source": "x" * 301, "target": "t"}),
    ("/lineage/edges", {"source": "s", "target": "t", "job": "x" * 301}),
])
def test_too_long_input_is_422_on_every_database(client, path, body):
    r = client.post(f"{API}{path}", headers=H(), json=body)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


def test_too_long_values_in_updates_and_openlineage_are_422(client):
    a = _asset(client)
    assert client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"steward": "x" * 201}).status_code == 422
    assert client.put(f"{API}/assets/{a['id']}/ownership", headers=ADMIN, json={"owner": "x" * 201}).status_code == 422
    assert client.post(f"{API}/lineage/openlineage", headers=ADMIN, json={
        "run": {"runId": "x" * 101}, "job": {"name": "j"}, "inputs": [], "outputs": []}).status_code == 422
    assert client.post(f"{API}/lineage/openlineage", headers=ADMIN, json={
        "run": {"runId": "r"}, "job": {"name": "j"}, "inputs": [{"name": "x" * 301}], "outputs": [{"name": "o"}]}).status_code == 422


def test_generated_issue_titles_and_audit_entity_ids_fit_their_columns(client):
    """入力は上限内でも、連結した値が列の長さを超える場合に 500 にならないこと。"""
    long_qn = "q" * 300
    a = _asset(client, long_qn, name="n" * 200)
    r = client.post(f"{API}/quality/assets/{a['id']}/rules", headers=ADMIN | {"X-Actor": "st"}, json={
        "name": "r" * 200, "dimension": "completeness", "rule_type": "not_null", "column": "c"})
    assert r.status_code == 201, r.text
    run = client.post(f"{API}/quality/assets/{a['id']}/runs", headers=ADMIN | {"X-Actor": "st"}, json={"records": [{"c": None}]})
    assert run.status_code == 201, run.text
    issue = client.get(f"{API}/issues", headers=COMMITTEE, params={"kind": "quality"}).json()[0]
    assert len(issue["title"]) <= 300 and issue["title"].startswith("[DQ]")
    # lineage の監査 entity_id は source->target (最大 602 文字)
    a2, a3 = "s" * 300, "t" * 300
    assert client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": a2, "target": a3}).status_code == 201
    ev = client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "lineage.edge"}).json()[0]
    assert len(ev["entity_id"]) <= 300 and ev["payload"]["source"] == a2
    ol = {"run": {"runId": "r"}, "job": {"namespace": "n" * 300, "name": "j" * 300}, "inputs": [], "outputs": []}
    assert client.post(f"{API}/lineage/openlineage", headers=ADMIN, json=ol).status_code == 202
    assert client.post(f"{API}/lineage/openlineage", headers=ADMIN, json={**ol, "inputs": [{"name": "i"}], "outputs": [{"name": "o"}]}).status_code == 202
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"]


def test_actor_length_fits_audit_column(client):
    ok = "a" * 196  # "dev:" を付けても actor 列 (200) に収まる
    r = client.post(f"{API}/domains", headers=ADMIN | {"X-Actor": ok}, json={"name": "d1"})
    assert r.status_code == 201, r.text
    assert client.post(f"{API}/domains", headers=ADMIN | {"X-Actor": "a" * 197}, json={"name": "d2"}).status_code == 401


def test_long_jwt_subject_is_rejected():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from edgf.auth import AuthError, JwtAuthenticator, JwtConfig
    from starlette.requests import Request

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def signing_key(self, token):
            return key.public_key()

    auth = JwtAuthenticator(JwtConfig(issuer="i", audience="a"), Keys())
    now = int(datetime.now(timezone.utc).timestamp())
    for sub, accepted in (("x" * 200, True), ("x" * 201, False)):
        t = jwt.encode({"iss": "i", "aud": "a", "exp": now + 60, "sub": sub}, key, algorithm="RS256")
        req = Request({"type": "http", "headers": [(b"authorization", f"Bearer {t}".encode())]})
        if accepted:
            assert auth.authenticate(req).subject == sub
        else:
            with pytest.raises(AuthError):
                auth.authenticate(req)


def test_nul_character_is_not_a_server_error(client):
    r = client.post(f"{API}/domains", headers=ADMIN, json={"name": "a\u0000b"})
    assert r.status_code < 500, r.text
    assert client.get(f"{API}/assets", headers=ADMIN, params={"q": "a\x00b"}).status_code < 500
    if is_postgres:
        assert r.status_code == 422 and r.json()["type"] == "urn:edgf:problem:invalid-value"


# ---------------------------------------------------------------- タイムゾーン
JST = timezone(timedelta(hours=9))


def test_expires_at_with_offset_is_converted_to_utc(client):
    """+09:00 の日時を UTC に変換せず保存すると、期限が 9 時間ずれる。"""
    a = _asset(client, sensitivity="restricted")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    in_jst = expires.astimezone(JST).isoformat()
    g = client.post(f"{API}/assets/{a['id']}/access-grants", headers={"X-Actor": "o", "X-Roles": "", "X-Domains": ""},
                    json={"subject": "u", "purpose": "p", "expires_at": in_jst})
    assert g.status_code == 201, g.text
    stored = datetime.fromisoformat(g.json()["expires_at"])
    assert abs(stored - expires.replace(tzinfo=None)) < timedelta(seconds=2)
    ex = client.post(f"{API}/policies/exceptions", headers=ADMIN, json={
        "policy_id": "LIN-001", "asset_id": a["id"], "reason": "r", "expires_at": in_jst})
    assert ex.status_code == 201, ex.text
    assert abs(datetime.fromisoformat(ex.json()["expires_at"]) - expires.replace(tzinfo=None)) < timedelta(seconds=2)


def test_past_datetime_with_positive_offset_is_rejected(client):
    a = _asset(client, sensitivity="restricted")
    past_in_jst = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(JST).isoformat()
    r = client.post(f"{API}/assets/{a['id']}/access-grants", headers={"X-Actor": "o", "X-Roles": "", "X-Domains": ""},
                    json={"subject": "u", "purpose": "p", "expires_at": past_in_jst})
    assert r.status_code == 422
    r = client.post(f"{API}/policies/exceptions", headers=ADMIN, json={
        "policy_id": "LIN-001", "asset_id": a["id"], "reason": "r", "expires_at": past_in_jst})
    assert r.status_code == 422


def test_session_timezone_does_not_shift_stored_timestamps(client):
    """DB セッションのタイムゾーンが UTC でなくても、naive な UTC の値はずれない。"""
    a = _asset(client)
    created = datetime.fromisoformat(a["created_at"])
    if is_postgres:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
            stored = conn.execute(text("SELECT created_at FROM data_assets")).scalar_one()
        assert stored == created
    assert abs(created - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(minutes=1)


# ---------------------------------------------------------------- JSON / ilike
def test_json_round_trip_preserves_hash_chain(client):
    payload = {"z": 1, "a": {"日本語": "値", "n": 1.5, "b": True, "x": None, "l": [3, 1, 2]}, "big": 12345678901234}
    with SessionLocal() as s:
        ev = audit.record(s, "system", "json.check", "t", "1", payload)
        s.commit()
        seq = ev.seq
    with SessionLocal() as s:
        assert s.get(AuditEvent, seq).payload == payload
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"]


@pytest.mark.parametrize("tag, q, hit", [
    ('with"quote', 'with"quote', True),
    ("back\\slash", "back\\slash", True),
    ("100%", "100%", True),
    ("under_score", "under_score", True),
    ("日本語タグ", "日本語タグ", True),
    ("Case", "cAsE", True),
    ("partial-tag", "partial", False),  # タグは要素の完全一致
])
def test_tag_search_with_metacharacters(client, tag, q, hit):
    _asset(client, tags=[tag, "other"])
    assert bool(client.get(f"{API}/assets", headers=ADMIN, params={"q": q}).json()) is hit


@pytest.mark.parametrize("q", ["%", "_", "!", "\\", "'", '"', "%' OR 1=1 --"])
def test_like_metacharacters_are_literal(client, q):
    _asset(client, name="plain", description="plain")
    assert client.get(f"{API}/assets", headers=ADMIN, params={"q": q}).json() == []
    assert client.get(f"{API}/glossary/terms", headers=ADMIN, params={"q": q}).json() == []


def test_null_for_json_list_fields_does_not_break_the_asset(client):
    a = _asset(client, tags=["t"], controls=["c"], schema=[{"name": "x"}])
    r = client.patch(f"{API}/assets/{a['id']}", headers=ADMIN, json={"tags": None, "controls": None, "schema": None})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["t"] and r.json()["controls"] == ["c"] and r.json()["schema"][0]["name"] == "x"
    assert client.get(f"{API}/assets/{a['id']}", headers=ADMIN).status_code == 200


def test_results_have_deterministic_order(client):
    a = _asset(client)
    for name in ("r3", "r1", "r2"):
        client.post(f"{API}/quality/assets/{a['id']}/rules", headers=ADMIN | {"X-Actor": "st"}, json={
            "name": name, "dimension": "completeness", "rule_type": "not_null", "column": "c"})
    run = client.post(f"{API}/quality/assets/{a['id']}/runs", headers=ADMIN | {"X-Actor": "st"}, json={"records": [{"c": 1}]}).json()
    assert [x["name"] for x in run["results"]] == ["r1", "r2", "r3"]
    for dst in ("d2", "d3", "d1"):
        client.post(f"{API}/lineage/edges", headers=ADMIN, json={"source": "s", "target": dst})
    edges = client.get(f"{API}/lineage/s/downstream", headers=ADMIN).json()["edges"]
    assert [e["to"] for e in edges] == ["d2", "d3", "d1"]  # 登録順


# ---------------------------------------------------------------- 一意制約違反
def test_unique_violation_raises_integrity_error():
    from tests.conftest import reset_db
    from edgf import migrate
    reset_db()
    migrate.upgrade(engine)
    with SessionLocal() as s:
        s.add(Domain(name="dup"))
        s.commit()
    with SessionLocal() as s:
        s.add(Domain(name="dup"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
    reset_db()


def test_real_unique_violation_is_409_without_leaking_sql():
    from tests.conftest import reset_db
    from edgf import migrate
    from fastapi import Depends
    from sqlalchemy.orm import Session

    reset_db()
    migrate.upgrade(engine)
    app = create_app()

    @app.post("/_dup")
    def dup(db: Session = Depends(get_session)):
        db.add(Domain(name="dup"))
        db.flush()
        db.add(Domain(name="dup"))
        db.flush()

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/_dup")
        assert r.status_code == 409 and r.json()["type"] == "urn:edgf:problem:integrity-conflict"
        assert "domains" not in r.text and "INSERT" not in r.text
    # 失敗したトランザクションは巻き戻され、接続は再利用できる
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Domain)) == 0
    reset_db()


# ---------------------------------------------------------------- トランザクション
def test_audit_record_rolls_back_with_the_transaction(client):
    """再試行のセーブポイントがあっても、監査イベントだけが先に確定してはならない。"""
    before = _audit_count()
    with SessionLocal() as s:
        audit.record(s, "system", "tx.first", "t", "1", {})
        s.add(Domain(name="tx-domain"))
        s.flush()
        s.rollback()
    assert _audit_count() == before
    with SessionLocal() as s:
        assert s.get(Domain, "tx-domain") is None


def test_failed_request_leaves_no_partial_writes_and_chain_stays_valid(sales_asset, client):
    before = _audit_count()
    r = client.post(f"{API}/assets/{sales_asset['id']}/legal-hold", json={"legal_hold": True, "reason": "pii@example.com"})
    assert r.status_code == 422
    assert _audit_count() == before
    assert client.get(f"{API}/assets/{sales_asset['id']}").json()["legal_hold"] is False
    # PostgreSQL ではシーケンスに欠番ができるが、チェーンの検証には影響しない
    client.post(f"{API}/domains", json={"name": "after"})
    assert client.get(f"{API}/audit/verify").json()["valid"]


def test_denial_evidence_survives_rollback(client):
    r = client.post(f"{API}/domains", headers={"X-Actor": "x", "X-Roles": "", "X-Domains": ""}, json={"name": "denied"})
    assert r.status_code == 403
    assert client.get(f"{API}/audit/events", headers=COMMITTEE, params={"action": "authz.denied"}).json()[0]["entity_id"] == "denied"
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"]


def _audit_count() -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(AuditEvent)) or 0


# ---------------------------------------------------------------- 監査チェーンの衝突と再試行
def test_stale_read_is_detected_by_unique_constraint_and_retried(client, monkeypatch):
    """古い head を読んだ書き込みは prev_hash の一意制約で失敗し、再試行で回復する (全 DB で確認)。"""
    with SessionLocal() as s:
        first = audit.record(s, "system", "chain.first", "t", "1", {})
        s.commit()
        stale = first.prev_hash
    real = audit._latest_hash
    calls = []

    def stale_once(db):
        calls.append(1)
        return stale if len(calls) == 1 else real(db)

    monkeypatch.setattr(audit, "_latest_hash", stale_once)
    with SessionLocal() as s:
        s.add(Domain(name="kept"))
        s.flush()
        ev = audit.record(s, "system", "chain.retry", "t", "2", {})
        s.commit()
        assert ev.prev_hash == first.hash and len(calls) == 2
    with SessionLocal() as s:
        assert s.get(Domain, "kept") is not None  # 再試行しても同じトランザクションの変更は失われない
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"]


def test_retry_gives_up_with_503(client, monkeypatch):
    with SessionLocal() as s:
        first = audit.record(s, "system", "chain.first", "t", "1", {})
        s.commit()
        stale = first.prev_hash
    monkeypatch.setattr(audit, "_latest_hash", lambda db: stale)
    monkeypatch.setattr(audit, "MAX_WRITE_ATTEMPTS", 3)
    monkeypatch.setattr(audit, "_backoff", lambda attempt: None)
    r = client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "contended"})
    assert r.status_code == 503 and r.json()["type"] == "urn:edgf:problem:audit-chain-contention"
    monkeypatch.undo()
    assert client.get(f"{API}/domains", headers=COMMITTEE).json() == []
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"]


def test_non_chain_errors_are_not_retried():
    err = IntegrityError("stmt", {}, Exception("duplicate key value violates unique constraint \"uq_other\""))
    assert audit._is_chain_conflict(err) is False
    err = IntegrityError("stmt", {}, Exception('duplicate key ... "uq_audit_events_prev_hash"'))
    assert audit._is_chain_conflict(err) is True
    err = IntegrityError("stmt", {}, Exception("UNIQUE constraint failed: audit_events.prev_hash"))
    assert audit._is_chain_conflict(err) is True


@needs_real_concurrency
def test_concurrent_audit_writes_never_fork_the_chain(client):
    threads, per_thread = 8, 25
    barrier = threading.Barrier(threads)
    errors: list[Exception] = []

    def writer(n: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            try:
                with SessionLocal() as s:
                    audit.record(s, f"t{n}", "chain.concurrent", "t", f"{n}-{i}", {"n": n, "i": i})
                    s.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(writer, range(threads)))
    assert errors == []
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT seq, prev_hash, hash FROM audit_events WHERE action = 'chain.concurrent' ORDER BY seq")).all()
    assert len(rows) == threads * per_thread
    assert len({r.prev_hash for r in rows}) == len(rows)  # 分岐していない
    for earlier, later in zip(rows, rows[1:]):
        assert later.prev_hash == earlier.hash  # seq の順にチェーンがつながっている
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True


@needs_real_concurrency
def test_concurrent_multi_event_transactions_stay_atomic(client):
    """1 トランザクションで複数のイベントを書く場合も、チェーンの間に他の書き込みが割り込まない。"""
    threads, rounds = 6, 8

    def writer(n: int) -> None:
        for i in range(rounds):
            with SessionLocal() as s:
                audit.record(s, f"t{n}", "chain.multi", "t", f"{n}-{i}-a", {})
                audit.record(s, f"t{n}", "chain.multi", "t", f"{n}-{i}-b", {})
                s.commit()

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(writer, range(threads)))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT actor, entity_id FROM audit_events WHERE action = 'chain.multi' ORDER BY seq")).all()
    assert len(rows) == threads * rounds * 2
    for a, b in zip(rows[0::2], rows[1::2]):
        assert a.entity_id.endswith("-a") and b.entity_id.endswith("-b") and a.entity_id[:-2] == b.entity_id[:-2]
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True


@needs_real_concurrency
def test_rollback_under_contention_does_not_break_the_chain(client):
    def writer(n: int) -> None:
        for i in range(10):
            with SessionLocal() as s:
                audit.record(s, f"t{n}", "chain.mixed", "t", f"{n}-{i}", {})
                if i % 2:
                    s.rollback()
                else:
                    s.commit()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(writer, range(6)))
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'chain.mixed'")).scalar_one() == 30


def test_operational_error_is_503_without_leaking_sql():
    from sqlalchemy.exc import OperationalError

    app = create_app()

    @app.get("/_down")
    def down():
        raise OperationalError("SELECT secret FROM t", {}, Exception("deadlock detected"))

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/_down")
    assert r.status_code == 503 and r.json()["type"] == "urn:edgf:problem:database-unavailable"
    assert r.headers["retry-after"] == "1" and "secret" not in r.text and "deadlock" not in r.text


@needs_real_concurrency
def test_deadlock_resolves_quickly_and_keeps_chain_valid(client):
    """行ロックと監査チェーンの先頭を逆順で取り合うとデッドロックになる。

    再試行で粘らず片方を失敗させて、ライブロックせずに解消すること (deadlock_timeout は既定 1 秒)。
    """
    import time

    from sqlalchemy import update
    from sqlalchemy.exc import OperationalError

    with SessionLocal() as s:
        s.add(Domain(name="shared"))
        s.commit()
    barrier = threading.Barrier(2)
    outcome: dict[str, str] = {}

    def row_then_audit() -> None:
        with SessionLocal() as s:
            try:
                s.execute(update(Domain).where(Domain.name == "shared").values(description="t1"))
                barrier.wait()
                time.sleep(0.1)
                audit.record(s, "t1", "deadlock.check", "t", "1", {})
                s.commit()
                outcome["t1"] = "ok"
            except OperationalError:
                s.rollback()
                outcome["t1"] = "deadlock"

    def audit_then_row() -> None:
        with SessionLocal() as s:
            try:
                audit.record(s, "t2", "deadlock.check", "t", "2", {})
                barrier.wait()
                time.sleep(0.1)
                s.execute(update(Domain).where(Domain.name == "shared").values(description="t2"))
                s.commit()
                outcome["t2"] = "ok"
            except OperationalError:
                s.rollback()
                outcome["t2"] = "deadlock"

    started = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        for f in (pool.submit(row_then_audit), pool.submit(audit_then_row)):
            f.result(timeout=30)
    elapsed = time.monotonic() - started
    assert sorted(outcome.values()) == ["deadlock", "ok"], outcome
    assert elapsed < 10, elapsed  # ライブロックしていない
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True


@needs_real_concurrency
def test_concurrent_api_writes_all_succeed(client):
    def post(i: int):
        return client.post(f"{API}/domains", headers=COMMITTEE, json={"name": f"d{i}"}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(post, range(40)))
    assert codes == [201] * 40
    assert client.get(f"{API}/domains", headers=COMMITTEE).headers["x-total-count"] == "40"
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True


@needs_real_concurrency
def test_race_on_same_key_gives_one_success_and_conflicts(client):
    def post(_: int):
        return client.post(f"{API}/domains", headers=COMMITTEE, json={"name": "same"}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(post, range(8)))
    assert codes.count(201) == 1 and set(codes) <= {201, 409}, codes
    assert client.get(f"{API}/audit/verify", headers=COMMITTEE).json()["valid"] is True


@needs_real_concurrency
def test_race_on_same_source_keeps_xref_unique(client):
    master_asset(client)
    record = {"source_system": "crm", "key": "RACE", "attributes": {"full_name": "x"}}

    def post(_: int):
        return client.post(f"{MDM}/party/match", headers=HUB, json={"record": record}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(post, range(8)))
    assert set(codes) <= {200, 409}, codes
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(MdmXref)) == 1


def test_tests_run_on_the_required_database():
    """CI の PostgreSQL ジョブが、誤って SQLite で実行されていないことを確認する。"""
    import os
    required = os.environ.get("EDGF_REQUIRE_DIALECT")
    if required:
        assert engine.dialect.name == required
