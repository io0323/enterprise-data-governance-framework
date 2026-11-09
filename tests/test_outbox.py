"""Phase 12c (§16): Transactional Outbox と Webhook の配信."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select, text

from edgf.config import settings
from edgf.db import SessionLocal, engine
from edgf.models import OutboxEvent
from edgf.services import issues, outbox, webhooks
from edgf.services.audit import AuditPayloadError
from edgf.services.webhooks import UrllibTransport, sign, verify_signature
from tests.process_helpers import ADMIN, API, COMMITTEE, OWNER, U, asset, audit_actions, decide, request_access

SECRET_NAME = "EDGF_TEST_WEBHOOK_SECRET"
SECRET = "s" * 40
HOOK = f"{API}/webhooks"
URL = "https://hooks.example.test/edgf"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv(SECRET_NAME, SECRET)


class FakeTransport:
    """受信側の代わり。イベント ID で冪等に処理する (受信側の実装例)。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.received: list[dict] = []
        self.calls: list[tuple[str, bytes, dict]] = []
        self.processed: dict[str, int] = {}

    def send(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers)))
        event = json.loads(body)
        self.received.append(event)
        self.processed[event["id"]] = self.processed.get(event["id"], 0) + (0 if event["id"] in self.processed else 1)
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return 200


def _endpoint(client, **kw) -> dict:
    r = client.post(HOOK, headers=ADMIN, json={"url": URL, "secret_ref": f"env:{SECRET_NAME}", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _events(type_name=None) -> list[OutboxEvent]:
    with SessionLocal() as db:
        stmt = select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id)
        if type_name:
            stmt = stmt.where(OutboxEvent.type == type_name)
        return list(db.scalars(stmt))


def _expire_backoff():
    with engine.begin() as conn:
        conn.execute(text("UPDATE webhook_deliveries SET next_attempt_at = :t WHERE status = 'pending'"), {"t": datetime(2020, 1, 1)})


# ---------------------------------------------------------------- トランザクション
def test_event_commits_with_state_change(client):
    with SessionLocal() as db:
        issue = issues.open_issue(db, "dev:x", asset=None, kind="other", title="t")
        issue_id = issue.id
        db.commit()
    assert [e.subject for e in _events("edgf.issue.opened")] == [issue_id]


def test_rollback_discards_state_change_and_event(client):
    with SessionLocal() as db:
        issues.open_issue(db, "dev:x", asset=None, kind="other", title="t")
        db.rollback()
    assert _events("edgf.issue.opened") == []
    assert client.get(f"{API}/issues", headers=ADMIN).json() == []


def test_failed_request_leaves_no_event(client):
    """API の失敗でもイベントは残らない: Legal Hold で削除の承認を巻き戻す。"""
    from edgf.models import DataAsset
    from edgf.services import approvals
    a = asset(client, retention_days=1)
    with SessionLocal() as db:
        req = approvals.create(db, "system:jobs", type_name="retention_delete", requested_by="system:jobs", target_type="asset",
                               target_id=a["id"], asset=db.get(DataAsset, a["id"]))
        db.commit()
    assert client.post(f"{API}/assets/{a['id']}/legal-hold", headers=COMMITTEE, json={"legal_hold": True, "reason": "r"}).status_code == 200
    decide(client, req.id, OWNER, code=409)
    assert _events("edgf.approval.approved") == [] and _events("edgf.asset.delete_requested") == []


@pytest.mark.parametrize("data", [
    {"email": "x"}, {"note": "person@example.com"}, {"nested": {"phone": "x"}}, {"items": ["090-1234-5678"]},
])
def test_pii_in_event_payload_is_rejected(client, data):
    with SessionLocal() as db:
        with pytest.raises(AuditPayloadError):
            outbox.emit(db, "issue.opened", "x", data)
        db.rollback()
    assert _events("edgf.issue.opened") == []


def test_cloudevents_format(client):
    with SessionLocal() as db:
        event = outbox.emit(db, "policy.published", "7", {"version": 7})
        db.commit()
        ce = outbox.to_cloudevent(event)
    assert ce["specversion"] == "1.0" and ce["id"] == event.id and ce["source"] == "urn:edgf:control-plane"
    assert ce["type"] == "edgf.policy.published" and ce["subject"] == "7" and ce["datacontenttype"] == "application/json"
    assert ce["time"].endswith("Z") and ce["data"] == {"version": 7}


def test_request_id_extension(client):
    r = client.post(f"{API}/issues", headers={**ADMIN, "X-Request-ID": "req-outbox-1"}, json={"kind": "other", "title": "t"})
    assert r.status_code == 201
    event = _events("edgf.issue.opened")[0]
    assert event.request_id == "req-outbox-1" and outbox.to_cloudevent(event)["edgfrequestid"] == "req-outbox-1"


def test_domain_events_are_emitted(client):
    a = asset(client)
    req = request_access(client, a["id"])
    decide(client, req["id"], OWNER)
    grant = client.get(f"{API}/assets/{a['id']}/access-grants", headers=OWNER).json()[0]
    assert client.delete(f"{API}/assets/{a['id']}/access-grants/{grant['id']}", headers=OWNER).status_code == 200
    share = request_access(client, a["id"], who="bob", type_name="external_share", recipient="取引先A")
    decide(client, share["id"], OWNER)
    decide(client, share["id"], COMMITTEE)
    issue = client.post(f"{API}/issues", headers=ADMIN, json={"kind": "other", "title": "t", "asset_id": a["id"]}).json()
    assert client.patch(f"{API}/issues/{issue['id']}", headers=COMMITTEE, json={"status": "in_progress"}).status_code == 200
    assert client.post(f"{API}/issues/{issue['id']}/escalate", headers=COMMITTEE, json={"reason": "r"}).status_code == 200
    assert client.post(f"{API}/assets/{a['id']}/lifecycle", headers=COMMITTEE, json={"target_state": "deleted"}).status_code == 200
    types = {e.type for e in _events()}
    assert {"edgf.approval.requested", "edgf.approval.approved", "edgf.access.granted", "edgf.access.revoked", "edgf.share.approved",
            "edgf.issue.opened", "edgf.issue.updated", "edgf.issue.escalated", "edgf.asset.delete_requested"} <= types


def test_policy_and_contract_events(client):
    from tests.mesh_helpers import published, setup_contract
    ctx = setup_contract(client)
    v = published(client, ctx["contract"]["id"])
    assert _events("edgf.contract.published")[0].data["contract_version_id"] == v["id"]
    original = client.get(f"{API}/policies/bundles/1", headers=ADMIN).json()["files"]["global.yaml"]
    p = client.post(f"{API}/policies/bundles", headers=U("proposer", "committee"), json={"changes": {"global.yaml": original + "\n# comment\n"}, "reason": "r"}).json()
    decide(client, p["approval_id"], COMMITTEE)
    assert client.post(f"{API}/policies/bundles/{p['proposal_version']}/publish", headers=COMMITTEE).status_code == 200
    assert _events("edgf.policy.published")[0].data["version"] == p["proposal_version"]


def test_erasure_dispatch_pushes_without_subject_data(client):
    raw = "Push.Person@Example.com"
    src = asset(client, "crm.people", sensitivity="confidential", contains_pii=True, retention_days=1, schema=[{"name": "email", "identifier_type": "email"}])
    r = client.post(f"{API}/privacy/erasure-requests", headers=ADMIN, json={"identifier_type": "email", "identifier": raw}).json()
    subject_ref = r["subject_ref"]
    for action in ("scope", "approve", "dispatch"):
        assert client.post(f"{API}/privacy/erasure-requests/{r['id']}/{action}", headers=COMMITTEE).status_code == 200
    events = _events("edgf.erasure.task.dispatched")
    assert len(events) == 1 and events[0].data["asset_id"] == src["id"]
    text_form = json.dumps(events[0].data)
    assert raw.lower() not in text_form.lower() and subject_ref not in text_form
    # 既存の pull 型の API はそのまま使える
    assert len(client.get(f"{API}/privacy/erasure-tasks", headers=U("asset-custodian"), params={"status": "pending"}).json()) == 1


# ---------------------------------------------------------------- 署名
def test_signature_verification():
    body = b'{"id":"1"}'
    now = 1_800_000_000
    header = sign(SECRET, body, now)
    assert verify_signature(SECRET, header, body, now=now)
    assert verify_signature(SECRET, header, body, now=now + 299)
    assert not verify_signature(SECRET, header, body, now=now + 301)  # 古い
    assert not verify_signature(SECRET, header, b'{"id":"2"}', now=now)  # 改ざん
    assert not verify_signature("x" * 40, header, body, now=now)  # 鍵が違う
    for broken in ("", "t=abc,v1=x", "v1=abc", "garbage"):
        assert not verify_signature(SECRET, broken, body, now=now)


def test_delivery_is_signed_and_verifiable(client):
    _endpoint(client)
    with SessionLocal() as db:
        event = outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()
    transport = FakeTransport()
    counts = webhooks.deliver_due(transport)
    assert counts["delivered"] == 1
    url, body, headers = transport.calls[0]
    assert url == URL and headers["Content-Type"] == "application/cloudevents+json"
    assert verify_signature(SECRET, headers["X-EDGF-Signature"], body)
    assert json.loads(body)["id"] == event.id


# ---------------------------------------------------------------- 再試行とデッドレター
def test_retry_backoff_and_dead_letter(client):
    _endpoint(client)
    with SessionLocal() as db:
        event = outbox.emit(db, "policy.published", "1", {"version": 1})
        event_id = event.id
        db.commit()
    transport = FakeTransport([500] * 100)
    assert webhooks.deliver_due(transport)["retried"] == 1
    # バックオフの間は再送しない
    assert webhooks.deliver_due(transport)["retried"] == 0 and len(transport.calls) == 1
    for _ in range(settings.webhook_max_attempts - 2):
        _expire_backoff()
        assert webhooks.deliver_due(transport)["retried"] == 1
    assert client.get(f"{API}/outbox/dead-letters", headers=ADMIN).json() == []
    _expire_backoff()
    assert webhooks.deliver_due(transport)["dead"] == 1
    assert len(transport.calls) == settings.webhook_max_attempts
    dead = client.get(f"{API}/outbox/dead-letters", headers=ADMIN).json()
    assert [(d["event_id"], d["status"], d["last_error"]) for d in dead] == [(event_id, "dead", "http:500")]
    assert any(e["action"] == "outbox.dead_letter" for e in audit_actions(client))
    # デッドレターは自動では再送しない
    _expire_backoff()
    assert webhooks.deliver_due(transport)["retried"] == 0
    # 手動の再送: 同じイベント ID で届く
    assert client.post(f"{API}/outbox/dead-letters/{dead[0]['id']}/redeliver", headers=U("x", "committee")).status_code == 403
    assert client.get(f"{API}/outbox/dead-letters", headers=U("x", "committee")).status_code == 403
    r = client.post(f"{API}/outbox/dead-letters/{dead[0]['id']}/redeliver", headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["attempts"] == 0
    assert client.post(f"{API}/outbox/dead-letters/{dead[0]['id']}/redeliver", headers=ADMIN).status_code == 409
    assert client.post(f"{API}/outbox/dead-letters/none/redeliver", headers=ADMIN).status_code == 404
    ok = FakeTransport()
    assert webhooks.deliver_due(ok)["delivered"] == 1
    assert ok.received[0]["id"] == event_id
    assert client.get(f"{API}/outbox/dead-letters", headers=ADMIN).json() == []


def test_backoff_is_exponential_and_capped():
    assert [webhooks._backoff(n).total_seconds() for n in (1, 2, 3)] == [60, 120, 240]
    assert webhooks._backoff(30).total_seconds() == settings.webhook_backoff_max_seconds


def test_idempotent_receive_by_event_id(client):
    """届いたのに応答が失われると再送される (at-least-once)。受信側はイベント ID で 1 回だけ処理する。"""
    _endpoint(client)
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()
    transport = FakeTransport([TimeoutError("lost response"), 200])
    assert webhooks.deliver_due(transport)["retried"] == 1
    _expire_backoff()
    assert webhooks.deliver_due(transport)["delivered"] == 1
    assert len(transport.received) == 2 and transport.received[0]["id"] == transport.received[1]["id"]
    assert list(transport.processed.values()) == [1]


def test_exception_detail_is_not_stored(client):
    _endpoint(client)
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()
    webhooks.deliver_due(FakeTransport([ConnectionError("https://user:token@internal/secret")]))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT last_error FROM webhook_deliveries")).scalar() == "error:ConnectionError"


def test_missing_secret_never_sends_unsigned(client, monkeypatch):
    _endpoint(client)
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()
    monkeypatch.delenv(SECRET_NAME)
    transport = FakeTransport()
    assert webhooks.deliver_due(transport)["retried"] == 1 and transport.calls == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT last_error FROM webhook_deliveries")).scalar() == "secret_unavailable"


# ---------------------------------------------------------------- フィルタと管理
def test_filter_by_event_type(client):
    _endpoint(client, event_types=["edgf.issue.*"])
    _endpoint(client, url="https://other.example.test/x", event_types=["edgf.policy.published"])
    _endpoint(client, url="https://all.example.test/x")
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        issues.open_issue(db, "dev:x", asset=None, kind="other", title="t")
        db.commit()
    transport = FakeTransport()
    assert webhooks.deliver_due(transport)["delivered"] == 4
    urls = sorted((u, json.loads(b)["type"]) for u, b, _ in transport.calls)
    assert urls == [("https://all.example.test/x", "edgf.issue.opened"), ("https://all.example.test/x", "edgf.policy.published"),
                    (URL, "edgf.issue.opened"), ("https://other.example.test/x", "edgf.policy.published")]
    assert webhooks.matches([], "x") and webhooks.matches(["edgf.*"], "edgf.a") and not webhooks.matches(["edgf.a"], "edgf.b")


def test_inactive_endpoint_holds_deliveries(client):
    e = _endpoint(client)
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()
    assert webhooks.deliver_due(FakeTransport([500]))["retried"] == 1
    assert client.patch(f"{HOOK}/{e['id']}", headers=ADMIN, json={"active": False}).json()["active"] is False
    _expire_backoff()
    transport = FakeTransport()
    assert webhooks.deliver_due(transport)["delivered"] == 0 and transport.calls == []
    # 無効の間のイベントは割り当てない
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "2", {"version": 2})
        db.commit()
    assert webhooks.deliver_due(transport)["fanned_out"] == 0
    assert client.patch(f"{HOOK}/{e['id']}", headers=ADMIN, json={"active": True}).json()["active"] is True
    assert webhooks.deliver_due(transport)["delivered"] == 1


def test_endpoint_management(client):
    body = {"url": URL, "secret_ref": f"env:{SECRET_NAME}"}
    for who in (COMMITTEE, U("x", "owner", "*"), U("y")):
        assert client.post(HOOK, headers=who, json=body).status_code == 403
        assert client.get(HOOK, headers=who).status_code == 403
    for bad in ({"secret_ref": "plain-secret-value"}, {"secret_ref": "env:MISSING_SECRET_NAME"}, {"url": "ftp://x/y"},
                {"url": "https://user:pw@x/y"}, {"url": "not a url"}, {"secret": "x"}):
        assert client.post(HOOK, headers=ADMIN, json={**body, **bad}).status_code == 422, bad
    e = _endpoint(client, description="監視", event_types=["edgf.*"])
    assert e["secret_ref"] == f"env:{SECRET_NAME}" and SECRET not in json.dumps(e)
    assert client.get(f"{HOOK}/{e['id']}", headers=ADMIN).json()["id"] == e["id"]
    assert client.get(f"{HOOK}/{e['id']}", headers=COMMITTEE).status_code == 403
    assert client.get(f"{HOOK}/none", headers=ADMIN).status_code == 404
    assert client.get(HOOK, headers=ADMIN).headers["X-Total-Count"] == "1"
    assert client.patch(f"{HOOK}/{e['id']}", headers=COMMITTEE, json={"active": False}).status_code == 403
    assert client.patch(f"{HOOK}/{e['id']}", headers=ADMIN, json={"url": "ftp://x"}).status_code == 422
    assert client.patch(f"{HOOK}/{e['id']}", headers=ADMIN, json={"event_types": ["edgf.issue.*"], "url": None}).json()["url"] == URL
    assert SECRET not in json.dumps(audit_actions(client))
    assert {"webhook.create", "webhook.update"} <= {x["action"] for x in audit_actions(client)}


def test_short_secret_is_rejected(client, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, "short")
    assert client.post(HOOK, headers=ADMIN, json={"url": URL, "secret_ref": f"env:{SECRET_NAME}"}).status_code == 422


def test_production_blocks_internal_destinations(monkeypatch):
    object.__setattr__(settings, "env", "production")
    try:
        for url in ("http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/latest", "https://localhost/x"):
            with pytest.raises(webhooks.WebhookConfigError):
                webhooks.check_destination(url)
        monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))])
        webhooks.check_destination("https://example.com/x")
        monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("dns")))
        with pytest.raises(webhooks.WebhookConfigError):
            webhooks.check_destination("https://example.com/x")
    finally:
        object.__setattr__(settings, "env", "test")


def test_blocked_destination_at_send_is_retried_not_sent(client):
    _endpoint(client)
    with SessionLocal() as db:
        outbox.emit(db, "policy.published", "1", {"version": 1})
        db.commit()

    class Blocking:
        def send(self, *a, **k):
            raise webhooks.WebhookConfigError("blocked")
    assert webhooks.deliver_due(Blocking())["retried"] == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT last_error FROM webhook_deliveries")).scalar() == "destination_blocked"


# ---------------------------------------------------------------- 実際の送信 (標準ライブラリ)
class _Handler(BaseHTTPRequestHandler):
    received: list[tuple[str, bytes, dict]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.received.append((self.path, body, dict(self.headers)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/internal")
        else:
            self.send_response(500 if self.path == "/fail" else 204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_server():
    _Handler.received = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_urllib_transport_end_to_end(http_server):
    t = UrllibTransport()
    assert t.send(f"{http_server}/ok", b"{}", {"Content-Type": "application/json"}, 5) == 204
    assert t.send(f"{http_server}/fail", b"{}", {}, 5) == 500
    # リダイレクトは追わない
    assert t.send(f"{http_server}/redirect", b"{}", {}, 5) == 302
    assert [r[0] for r in _Handler.received] == ["/ok", "/fail", "/redirect"]
