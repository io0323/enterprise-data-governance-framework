"""Phase 15a: Data Plane の SDK (clients/python, edgf-client)."""
from __future__ import annotations

import json

import httpx
import pytest

from edgf_client import (ClientCredentials, Conflict, EdgfClient, Forbidden, NotFound, ProblemError, ServerError, ServiceUnavailable, StaticToken,
                         TokenError, TransportError, Unauthenticated, ValidationFailed)
from edgf_client.errors import problem_from_response
from tests.test_auth import jwks, jwt_client, keys  # noqa: F401  (fixtures)

BASE = "https://cp.example.test"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def idp(expires_in=300, status=200, body=None, log=None):
    def handler(request):
        if log is not None:
            log.append(dict(httpx.QueryParams(request.content.decode())))
        if status != 200:
            return httpx.Response(status, json={"error": "invalid_client"})
        return httpx.Response(200, json=body if body is not None else {"access_token": f"tok-{len(log or [])}", "expires_in": expires_in})
    return httpx.Client(transport=httpx.MockTransport(handler))


def api(handler, credentials=None, **kw):
    sleeps = []
    client = EdgfClient(BASE, credentials or StaticToken("static"), http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, **kw)
    return client, sleeps


def problem(status, slug, **ext):
    return httpx.Response(status, json={"type": f"urn:edgf:problem:{slug}", "title": "t", "status": status, "detail": "d", "request_id": "rid", **ext},
                          headers={"content-type": "application/problem+json"})


# ---------------------------------------------------------------- トークン
def test_token_is_cached_and_refreshed_before_expiry():
    log, clock = [], Clock()
    creds = ClientCredentials("https://idp/token", "dp-1", "s3cret", scope="edgf", audience="cp", refresh_margin=60, http=idp(300, log=log), clock=clock)
    assert creds.token() == "tok-1" and creds.token() == "tok-1"
    assert log[0] == {"grant_type": "client_credentials", "client_id": "dp-1", "client_secret": "s3cret", "scope": "edgf", "audience": "cp"}
    clock.now += 239
    assert creds.token() == "tok-1"
    clock.now += 1  # 有効期限の 60 秒前
    assert creds.token() == "tok-2" and creds.fetch_count == 2


def test_secret_is_not_in_repr_or_errors():
    creds = ClientCredentials("https://idp/token", "dp-1", "s3cret-value", http=idp(status=401))
    with pytest.raises(TokenError) as e:
        creds.token()
    assert "s3cret-value" not in str(e.value) and "s3cret-value" not in repr(creds)


@pytest.mark.parametrize("body", [{}, {"access_token": ""}])
def test_token_response_without_token(body):
    with pytest.raises(TokenError):
        ClientCredentials("https://idp/token", "c", "s", http=idp(body=body)).token()


def test_token_endpoint_unreachable():
    def down(request):
        raise httpx.ConnectError("down")
    with pytest.raises(TokenError):
        ClientCredentials("https://idp/token", "c", "s", http=httpx.Client(transport=httpx.MockTransport(down))).token()


def test_401_refreshes_the_token_once():
    log = []
    creds = ClientCredentials("https://idp/token", "c", "s", http=idp(log=log))
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return problem(401, "unauthenticated") if len(seen) == 1 else httpx.Response(200, json={"ok": True})
    client, _ = api(handler, creds)
    assert client.request("GET", "/x").json() == {"ok": True}
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


def test_401_twice_raises():
    client, _ = api(lambda r: problem(401, "unauthenticated"), ClientCredentials("https://idp/token", "c", "s", http=idp(log=[])))
    with pytest.raises(Unauthenticated):
        client.request("GET", "/x")


# ---------------------------------------------------------------- 再試行
def test_get_is_retried_on_transient_errors():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("reset")
        if len(calls) == 2:
            return problem(503, "database-unavailable")
        return httpx.Response(200, json=[])
    client, sleeps = api(handler)
    assert client.erasure_tasks() == [] and len(calls) == 3 and len(sleeps) == 2


def test_non_idempotent_post_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("reset")
    client, sleeps = api(handler)
    with pytest.raises(TransportError):
        client.request("POST", "/api/v1/domains", json={"name": "x"})
    assert len(calls) == 1 and sleeps == [] and "idempotency-key" not in calls[0].headers
    calls.clear()

    def busy(request):
        calls.append(request)
        return problem(503, "database-unavailable")
    client, sleeps = api(busy)
    with pytest.raises(ServiceUnavailable):
        client.request("POST", "/api/v1/domains", json={"name": "x"})
    assert len(calls) == 1


def test_send_retries_with_the_same_idempotency_key():
    keys = []

    def handler(request):
        keys.append(request.headers.get("idempotency-key"))
        if len(keys) < 3:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(201, json={"id": "run-1"})
    client, sleeps = api(handler)
    assert client.submit_quality_run("a-1", [{"x": 1}]) == {"id": "run-1"}
    assert len(keys) == 3 and len(set(keys)) == 1 and keys[0]


def test_each_send_gets_a_new_key_unless_given():
    keys = []
    client, _ = api(lambda r: (keys.append(r.headers["idempotency-key"]), httpx.Response(200, json={}))[1])
    client.notify_refreshed("a")
    client.notify_refreshed("a")
    client.notify_refreshed("a", idempotency_key="fixed-key-1")
    assert keys[0] != keys[1] and keys[2] == "fixed-key-1"


def test_retry_after_and_backoff_limits():
    calls = []

    def handler(request):
        calls.append(1)
        return problem(429, "too-many-requests") if len(calls) < 3 else httpx.Response(200, json={})
    client, sleeps = api(handler, max_backoff=5)
    client.request("GET", "/x")
    assert len(sleeps) == 2 and all(0 < s <= 5 for s in sleeps)
    calls.clear()

    def with_header(request):
        calls.append(1)
        return httpx.Response(503, headers={"Retry-After": "120"}) if len(calls) == 1 else httpx.Response(200, json={})
    client, sleeps = api(with_header, max_backoff=7)
    client.request("GET", "/x")
    assert sleeps == [7.0]


def test_attempts_are_bounded():
    client, sleeps = api(lambda r: problem(502, "bad-gateway"), max_attempts=4)
    with pytest.raises(ServerError):
        client.request("GET", "/x")
    assert len(sleeps) == 3


def test_transport_errors_after_retries():
    def down(request):
        raise httpx.ConnectError("down")
    client, sleeps = api(down, max_attempts=2)
    with pytest.raises(TransportError):
        client.request("GET", "/x")
    assert len(sleeps) == 1


# ---------------------------------------------------------------- 例外
@pytest.mark.parametrize("status, slug, cls", [
    (401, "unauthenticated", Unauthenticated), (403, "forbidden", Forbidden), (404, "not-found", NotFound), (409, "conflict", Conflict),
    (422, "validation-error", ValidationFailed), (503, "auth-unavailable", ServiceUnavailable), (500, "internal-error", ServerError), (418, "teapot", ProblemError),
])
def test_problem_details_become_typed_exceptions(status, slug, cls):
    error = problem_from_response(problem(status, slug, action="a.b", errors=[{"loc": ["body"], "msg": "m"}]))
    assert type(error) is cls and error.status == status and error.slug == slug and error.request_id == "rid"
    assert error.extensions["action"] == "a.b" and "rid" in str(error)
    if cls is ValidationFailed:
        assert error.errors == [{"loc": ["body"], "msg": "m"}]


def test_non_problem_error_body():
    error = problem_from_response(httpx.Response(502, text="<html>bad gateway</html>"))
    assert isinstance(error, ServerError) and "bad gateway" in error.detail


def test_client_methods_build_the_documented_requests():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None, request.headers.get("idempotency-key")))
        return httpx.Response(200, json={})
    with api(handler)[0] as client:
        client.send_openlineage({"eventType": "COMPLETE"})
        client.ack_erasure_task("t1")
        client.complete_erasure_task("t1", records_deleted=2, evidence_hash="a" * 64)
        client.check_contract("c1", {"schema": []})
        client.mdm_match("party", {"source_system": "crm"}, [{"golden_id": "g"}])
        client.mdm_compose("party", "g", [{"source_system": "crm"}], {"name": "crm"})
        client.mdm_verify("party", "g", {"name": "x"}, version=2)
        client.rag_authorize("corpus", {"subject": "u"}, ["a", "b"])
        client.erasure_tasks("issued", limit=5)
        client.push_inventory("warehouse.sales", [{"qualified_name": "a.b"}])
        client.report_reachability("p1", True, observed_at="2026-11-11T00:00:00Z", detail="ok")
        client.push_usage([{"qualified_name": "a.b", "day": "2026-11-10", "query_count": 1, "user_count": 1}])
    paths = [(m, p) for m, p, _, _ in seen]
    assert paths == [("POST", "/api/v1/lineage/openlineage"), ("POST", "/api/v1/privacy/erasure-tasks/t1/ack"),
                     ("POST", "/api/v1/privacy/erasure-tasks/t1/complete"), ("POST", "/api/v1/mesh/contracts/c1/check"),
                     ("POST", "/api/v1/mdm/party/match"), ("POST", "/api/v1/mdm/party/golden-records/g/compose"),
                     ("POST", "/api/v1/mdm/party/golden-records/g/verify"), ("POST", "/api/v1/ai/rag/authorize"),
                     ("GET", "/api/v1/privacy/erasure-tasks"), ("POST", "/api/v1/discovery/snapshots"), ("POST", "/api/v1/mesh/ports/p1/reachability"), ("POST", "/api/v1/usage/daily")]
    # 送信系にだけ Idempotency-Key を付ける
    keyed = [p for m, p, _, k in seen if k]
    assert keyed == ["/api/v1/lineage/openlineage", "/api/v1/privacy/erasure-tasks/t1/ack", "/api/v1/privacy/erasure-tasks/t1/complete",
                     "/api/v1/mdm/party/match", "/api/v1/mdm/party/golden-records/g/compose", "/api/v1/discovery/snapshots",
                     "/api/v1/mesh/ports/p1/reachability", "/api/v1/usage/daily"]
    assert seen[2][2]["evidence_hash"] == "a" * 64 and seen[6][2] == {"attributes": {"name": "x"}, "version": 2}


# ---------------------------------------------------------------- 実際のサーバ (同じプロセス)
def test_against_the_server_duplicates_are_prevented(jwt_client, keys):
    """jwt モードのサーバに、Client Credentials で取得したトークンで送る。再送しても二重に登録されない。"""
    from tests.test_auth import token

    def token_endpoint(request):
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "client_credentials"
        return httpx.Response(200, json={"access_token": token(keys, sub=form["client_id"], roles=["admin"]), "expires_in": 600})
    creds = ClientCredentials("https://idp.example.test/token", "dp-pipeline", "secret", http=httpx.Client(transport=httpx.MockTransport(token_endpoint)))
    client = EdgfClient("http://testserver", creds, http=jwt_client)
    first = client.request("POST", "/api/v1/domains", json={"name": "sales"}, idempotency_key="dp-key-0001")
    again = client.request("POST", "/api/v1/domains", json={"name": "sales"}, idempotency_key="dp-key-0001")
    assert first.status_code == again.status_code == 201 and again.headers["idempotent-replayed"] == "true"
    assert len(client.request("GET", "/api/v1/domains").json()) == 1
    with pytest.raises(Conflict):
        client.request("POST", "/api/v1/domains", json={"name": "sales"})
    with pytest.raises(NotFound) as e:
        client.notify_refreshed("no-such-asset")
    assert e.value.request_id
    events = client.request("GET", "/api/v1/audit/events", params={"action": "domain.create"}).json()
    assert len(events) == 1 and events[0]["actor"] == "dp-pipeline"


def test_in_progress_is_retried_only_with_a_key():
    calls = []

    def handler(request):
        calls.append(request.headers.get("idempotency-key"))
        if len(calls) == 1:
            return problem(409, "idempotency-in-progress")
        return httpx.Response(201, json={"id": "x"})
    client, sleeps = api(handler)
    assert client.notify_refreshed("a") == {"id": "x"} and len(calls) == 2 and calls[0] == calls[1]
    calls.clear()
    client, _ = api(lambda r: (calls.append(1), problem(409, "conflict"))[1])
    with pytest.raises(Conflict):
        client.notify_refreshed("a")
    assert len(calls) == 1  # 通常の 409 は再送しない
