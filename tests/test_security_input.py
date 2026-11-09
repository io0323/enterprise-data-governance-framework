"""Phase 14c: JWT への攻撃、入力の異常、ログと監査への秘密の混入."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from sqlalchemy import text

from edgf.config import Settings
from edgf.db import engine
from edgf.errors import MAX_JSON_DEPTH, _JsonDepth
from edgf.main import create_app
from edgf.observability import JsonFormatter
from edgf.services.audit import verify_chain
from tests.test_auth import AUD, ISS, assert_401, bearer, jwks, jwt_client, keys, token  # noqa: F401  (fixtures)

API = "/api/v1"
ME = f"{API}/assets"
ADMIN = {"X-Actor": "root", "X-Roles": "admin"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forge(header: dict, claims: dict, signature: bytes = b"") -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(signature)}"


def _claims(**kw) -> dict:
    now = int(time.time())
    return {"iss": ISS, "aud": AUD, "sub": "attacker", "iat": now, "exp": now + 600, "roles": ["admin"], **kw}


def _public_pem(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


# ---------------------------------------------------------------- JWT への攻撃
@pytest.mark.parametrize("alg", ["none", "None", "NONE"])
def test_alg_none_is_rejected(jwt_client, alg):
    assert_401(jwt_client.get(ME, headers=bearer(_forge({"alg": alg, "kid": "k1", "typ": "JWT"}, _claims()))))


def test_hs256_signed_with_public_key_is_rejected(jwt_client, keys):
    """RS256/HS256 の取り違え: 公開鍵を HMAC の秘密鍵として署名したトークン。"""
    header = {"alg": "HS256", "kid": "k1", "typ": "JWT"}
    claims = _claims()
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = hmac.new(_public_pem(keys["k1"]), signing_input.encode(), hashlib.sha256).digest()
    assert_401(jwt_client.get(ME, headers=bearer(f"{signing_input}.{_b64(signature)}")))


def test_other_algorithm_with_correct_key_is_rejected(jwt_client, keys):
    """許可していないアルゴリズムは、鍵が正しくても受け付けない。"""
    forged = jwt.encode(_claims(), keys["k1"], algorithm="RS512", headers={"kid": "k1"})
    assert_401(jwt_client.get(ME, headers=bearer(forged)))


@pytest.mark.parametrize("header", [
    {"kid": "unknown"},                              # 未知の kid
    {"kid": "k1"},                                   # 正規の kid を騙る
    {"kid": "../../etc/passwd"},                     # パスの差し替え
    {"kid": "' OR 1=1 --"},                          # SQL の差し替え
    {"kid": "k1", "jku": "https://evil.test/jwks"},  # 外部の鍵の指定
    {"kid": "k1", "x5u": "https://evil.test/cert"},
])
def test_kid_replacement_with_rogue_key_is_rejected(jwt_client, keys, jwks, header):
    forged = jwt.encode(_claims(), keys["rogue"], algorithm="RS256", headers=header)
    before = jwks.fetches
    assert_401(jwt_client.get(ME, headers=bearer(forged)))
    # 取りに行くのは設定された JWKS だけ (初回の取得と、未知の kid による再取得で最大 2 回)
    assert jwks.fetches - before <= 2


def test_embedded_jwk_is_ignored(jwt_client, keys):
    rogue_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(keys["rogue"].public_key(), as_dict=True)
    forged = jwt.encode(_claims(), keys["rogue"], algorithm="RS256", headers={"kid": "k1", "jwk": rogue_jwk})
    assert_401(jwt_client.get(ME, headers=bearer(forged)))


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 3600},
    {"nbf": int(time.time()) + 3600},
    {"iss": "https://evil.test"},
    {"aud": "other-service"},
    {"exp": None},
    {"iss": None},
    {"aud": None},
    {"sub": None},
    {"sub": ""},
    {"sub": "x" * 201},
], ids=["expired", "nbf", "iss", "aud", "no-exp", "no-iss", "no-aud", "no-sub", "empty-sub", "long-sub"])
def test_claims_are_validated(jwt_client, keys, claims):
    assert_401(jwt_client.get(ME, headers=bearer(token(keys, **claims))))


@pytest.mark.parametrize("position", [0, 1, 2], ids=["header", "payload", "signature"])
def test_tampered_token_is_rejected(jwt_client, keys, position):
    parts = token(keys, roles=["consumer"]).split(".")
    if position == 1:
        parts[1] = _b64(json.dumps(_claims()).encode())  # 権限の昇格
    else:
        raw = bytearray(base64.urlsafe_b64decode(parts[position] + "=" * (-len(parts[position]) % 4)))
        raw[len(raw) // 2] ^= 0xFF
        parts[position] = _b64(bytes(raw))
    assert_401(jwt_client.get(ME, headers=bearer(".".join(parts))))


def test_huge_token_is_rejected_before_key_lookup(jwt_client, keys, jwks):
    before = jwks.fetches
    assert_401(jwt_client.get(ME, headers=bearer(token(keys, padding="x" * 20_000))))
    assert_401(jwt_client.get(ME, headers=bearer("a" * 100_000)))
    assert jwks.fetches == before


@pytest.mark.parametrize("value", ["garbage", "a.b", "a.b.c", "....", "Bearer", "é.é.é"])
def test_malformed_tokens_are_401(jwt_client, value):
    assert_401(jwt_client.get(ME, headers={"Authorization": f"Bearer {value}".encode("latin-1")}))


def test_valid_token_still_works_as_control(jwt_client, keys):
    assert jwt_client.get(ME, headers=bearer(token(keys))).status_code == 200


# ---------------------------------------------------------------- 本文の大きさ
@pytest.fixture()
def small_body_client(client):
    with TestClient(create_app(Settings(auth_mode="dev", env="test", max_body_bytes=1024)), headers=ADMIN) as c:
        yield c


def _assert_413(r):
    assert r.status_code == 413
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:edgf:problem:payload-too-large" and body["request_id"] == r.headers["x-request-id"]


def test_body_over_limit_is_413(small_body_client):
    _assert_413(small_body_client.post(f"{API}/domains", json={"name": "x" * 2000}))


def test_body_at_limit_is_processed(small_body_client):
    raw = json.dumps({"name": "ok"}).encode().ljust(1024)
    assert small_body_client.post(f"{API}/domains", content=raw, headers={"Content-Type": "application/json"}).status_code == 201


def test_streamed_body_without_length_is_counted(small_body_client):
    def chunks():
        yield b'{"name": "'
        for _ in range(100):
            yield b"x" * 100
        yield b'"}'

    _assert_413(small_body_client.post(f"{API}/domains", content=chunks(), headers={"Content-Type": "application/json"}))


def test_default_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("EDGF_MAX_BODY_BYTES", "77")
    assert Settings().max_body_bytes == 77


def test_limit_applies_before_authentication(small_body_client):
    _assert_413(small_body_client.post(f"{API}/domains", json={"name": "x" * 2000}, headers={"X-Actor": ""}))


# ---------------------------------------------------------------- 異常な入力 (500 にならない、Problem で返す)
def _problem(r, *codes):
    assert r.status_code in codes, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("levels", [100, 1000, 100_000])
def test_deeply_nested_json_is_not_a_server_error(client, levels):
    raw = b'{"name":"n","x":' + b"[" * levels + b"]" * levels + b"}"
    r = client.post(f"{API}/domains", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code < 500, r.text


@pytest.mark.parametrize("levels", [100, 1000, 100_000])
def test_deeply_nested_free_form_field_is_not_a_server_error(client, levels):
    """自由形式の項目 (params) に深い入れ子を入れても、保存と監査の連鎖が壊れない。"""
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales"})
    created = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "sales.a", "name": "a", "domain": "sales", "data_class": "master", "sensitivity": "internal", "owner": "alice"})
    assert created.status_code == 201
    nested = '{"a":' * levels + "1" + "}" * levels
    raw = ('{"name":"r","dimension":"completeness","rule_type":"not_null","column":"c","params":' + nested + "}").encode()
    r = client.post(f"{API}/quality/assets/{created.json()['id']}/rules", content=raw, headers={"Content-Type": "application/json", "X-Roles": "owner", "X-Domains": "*", "X-Actor": "alice"})
    assert r.status_code < 500, r.text
    assert client.get(f"{API}/audit/verify").json()["valid"] is True


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00", b'{"name":"\xe3\x81"}', b"\xc0\xaf", '{"name":"あ"}'.encode("shift_jis"), '{"name":"あ"}'.encode("utf-16")],
                         ids=["bom", "truncated", "overlong", "shift-jis", "utf-16"])
def test_invalid_encoding_is_rejected(client, raw):
    r = client.post(f"{API}/domains", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code < 500, r.text
    if r.status_code >= 400:
        _problem(r, 400, 422)


@pytest.mark.parametrize("value", ["a\u0000b", "\ud800", "\udfff", "a‮b"], ids=["nul", "high-surrogate", "low-surrogate", "bidi"])
def test_control_characters_in_strings(client, value):
    raw = json.dumps({"name": value, "description": value}).encode()
    r = client.post(f"{API}/domains", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code < 500, r.text


@pytest.mark.parametrize("raw", [b"", b"null", b"[]", b"1", b"{", b'{"name":', b"NaN", b'{"name":NaN}', b'{"name":1e999}', b'{"name":"x","name":"y","name":{}}'])
def test_malformed_json_is_problem(client, raw):
    r = client.post(f"{API}/domains", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code < 500, r.text
    if r.status_code >= 400:
        assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", ["/assets/%00", "/assets/%ff%fe", "/assets/..%2f..%2fetc%2fpasswd", "/lineage/%00/upstream", "/assets/by-name/%F0%9F%92%A9", "/assets/" + "x" * 5000])
def test_odd_paths_are_not_server_errors(client, path):
    assert client.get(f"{API}{path}").status_code < 500


@pytest.mark.parametrize("params", [{"limit": "-1"}, {"limit": "99999999999999999999"}, {"offset": "-1"}, {"limit": "abc"}, {"q": "%' OR 1=1 --"}, {"q": "\x00"}, {"q": "x" * 10000}, {"owner": "'; DROP TABLE data_assets; --"}])
def test_odd_query_parameters_are_not_server_errors(client, params):
    assert client.get(f"{API}/assets", params=params).status_code < 500
    assert client.get(f"{API}/assets").status_code == 200


def test_sql_metacharacters_do_not_change_results(client):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales"})
    assert client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "sales.a", "name": "a", "domain": "sales", "data_class": "master", "sensitivity": "internal", "owner": "alice"}).status_code == 201
    assert client.get(f"{API}/assets", params={"q": "' OR '1'='1"}).json() == []
    assert client.get(f"{API}/assets", params={"owner": "alice' OR '1'='1"}).json() == []
    assert len(client.get(f"{API}/assets", params={"owner": "alice"}).json()) == 1


def test_forged_request_id_does_not_inject_into_logs(client):
    r = client.get(f"{API}/assets", headers={"X-Request-ID": 'x"}\n{"level":"FORGED"'})
    assert "FORGED" not in r.headers["x-request-id"]


def test_depth_limit_boundary(client):
    client.post(f"{API}/domains", headers=ADMIN, json={"name": "sales"})
    created = client.post(f"{API}/assets", headers=ADMIN, json={"qualified_name": "sales.a", "name": "a", "domain": "sales", "data_class": "master", "sensitivity": "internal", "owner": "alice"}).json()

    def post(levels):
        # 外側の {} と params の {} の分を引いて、全体の深さをちょうど levels にする
        nested = "[" * (levels - 1) + "]" * (levels - 1)
        raw = '{"name":"r","dimension":"completeness","rule_type":"not_null","column":"c","params":{"x":' + nested + "}}"
        return client.post(f"{API}/quality/assets/{created['id']}/rules", content=raw.encode(), headers={"Content-Type": "application/json", "X-Roles": "owner", "X-Domains": "*", "X-Actor": "alice"})

    assert post(MAX_JSON_DEPTH - 1).status_code == 201
    too_deep = post(MAX_JSON_DEPTH + 1)
    assert too_deep.status_code == 422 and too_deep.json()["type"] == "urn:edgf:problem:json-too-deep"


@pytest.mark.parametrize("raw", [
    b'{"a":"[[[[[[[[[[[[[["}',       # 文字列の中の括弧は数えない
    b'{"a":"\\"[[[[[[[[[[[["}',     # エスケープされた引用符
    b'{"a":"\\\\"}',               # エスケープされた \ の次の引用符は文字列の終わり
])
def test_json_depth_ignores_strings(raw):
    scanner = _JsonDepth()
    assert scanner.feed(raw) == 1 and scanner.depth == 0 and not scanner.in_string


def test_json_depth_across_chunks():
    raw = b'{"a":"\\"[[","b":[[1]]}'
    for size in range(1, len(raw)):
        scanner = _JsonDepth()
        assert max(scanner.feed(raw[i:i + size]) for i in range(0, len(raw), size)) == 3, size
        assert scanner.depth == 0 and not scanner.in_string


# ---------------------------------------------------------------- ログと監査に秘密が出ない
@pytest.fixture()
def captured_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("edgf")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _audit_dump() -> str:
    with engine.connect() as conn:
        return "\n".join(str(tuple(row)) for row in conn.execute(text("SELECT actor, action, entity_type, entity_id, payload FROM audit_events")))


def test_tokens_never_reach_logs_or_audit(jwt_client, keys, captured_logs):
    good = token(keys)
    bad = token(keys, exp=int(time.time()) - 3600)
    forged = _forge({"alg": "none"}, _claims())
    assert jwt_client.post(f"{API}/domains", headers=bearer(good), json={"name": "sales"}).status_code == 201
    assert jwt_client.post(f"{API}/assets", headers=bearer(good), json={"qualified_name": "bad"}).status_code == 422
    assert jwt_client.post(f"{API}/lineage/edges", headers=bearer(token(keys, roles=["consumer"])), json={"source": "a", "target": "b"}).status_code == 403
    for t in (bad, forged):
        assert_401(jwt_client.get(ME, headers=bearer(t)))
    # クエリに置かれたトークンもログに出さない (パスだけを記録する)
    jwt_client.get(ME, params={"access_token": good}, headers=bearer(good))
    logged, audited = captured_logs.getvalue(), _audit_dump()
    assert "request completed" in logged and "domain" in audited
    for t in (good, bad, forged):
        for part in (t, t.split(".")[1], t.split(".")[-1] or t.split(".")[1]):
            assert part not in logged and part not in audited


def test_secrets_never_reach_logs_or_audit(client, monkeypatch, captured_logs):
    from edgf.services import subject_ref

    secret, subject_key, raw_subject = "webhook-secret-value-0001-" + "s" * 32, "subject-key-value-0002", "taro.yamada@example.com"
    monkeypatch.setenv("EDGF_SEC_TEST_SECRET", secret)
    monkeypatch.setattr(subject_ref, "settings", replace(subject_ref.settings, subject_key=subject_key))
    assert client.post(f"{API}/webhooks", headers=ADMIN, json={"url": "https://hooks.example.test/a", "secret_ref": "env:EDGF_SEC_TEST_SECRET"}).status_code == 201
    assert client.post(f"{API}/webhooks", headers=ADMIN, json={"url": "https://hooks.example.test/b", "secret_ref": secret}).status_code == 422
    assert client.post(f"{API}/privacy/purposes", headers=ADMIN, json={"id": "p1", "description": "d", "default_legal_basis": "consent"}).status_code == 201
    identifier = {"identifier_type": "email", "identifier": raw_subject, "purpose_id": "p1"}
    assert client.post(f"{API}/privacy/consents", headers=ADMIN, json={**identifier, "status": "granted"}).status_code == 201
    assert client.post(f"{API}/privacy/consents/check", headers=ADMIN, json=identifier).status_code == 200
    logged, audited = captured_logs.getvalue(), _audit_dump()
    for value in (secret, subject_key, raw_subject):
        assert value not in logged and value not in audited


def test_problem_responses_do_not_expose_internals(client):
    r = client.post(f"{API}/domains", headers=ADMIN, json={"name": "dup"})
    r = client.post(f"{API}/domains", headers=ADMIN, json={"name": "dup"})
    assert r.status_code in (409, 422)
    assert not any(word in r.text for word in ("SELECT", "INSERT", "sqlalchemy", "psycopg", "Traceback", 'File "'))


def test_chain_stays_valid_after_attacks(jwt_client, keys):
    assert_401(jwt_client.get(ME, headers=bearer(_forge({"alg": "none"}, _claims()))))
    assert jwt_client.post(f"{API}/lineage/edges", headers=bearer(token(keys, roles=["consumer"])), json={"source": "a", "target": "b"}).status_code == 403
    from edgf.db import SessionLocal

    with SessionLocal() as db:
        assert verify_chain(db)["valid"] is True
