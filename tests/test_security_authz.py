"""Phase 14c: ルートの網羅と、権限表の総当たり."""
from __future__ import annotations

import itertools
import re
import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI

from edgf import authz
from edgf.auth import ADMIN, COMMITTEE, CONSUMER, CUSTODIAN, OWNER, STEWARD, Principal, normalize_roles
from edgf.db import SessionLocal
from edgf.deps import get_principal
from edgf.errors import ProblemError
from edgf.main import create_app
from edgf.models import AuditEvent, DataAsset
from edgf.services.audit import verify_chain
from tests.security_helpers import api_routes, reachable_actions
from tests.security_world import ADMIN as ADMIN_H
from tests.security_world import ANALYST, build_world, candidates, fill_body

API = "/api/v1"
ROOT = Path(__file__).resolve().parents[1]
ROLES = (ADMIN, COMMITTEE, OWNER, STEWARD, CUSTODIAN, CONSUMER)
RELATIONS = (None, "owner", "steward", "custodian", "assignee", "requester")


# ---------------------------------------------------------------- ルートの網羅
def test_route_listing_is_complete():
    """列挙が空振りしていないことを確認する (FastAPI の内部構造が変わると 0 件で素通りしかねない)。"""
    app = create_app()
    routes = api_routes(app)
    documented = {(path, method.upper()) for path, item in app.openapi()["paths"].items() for method in item}
    listed = {(r.path, m) for r in routes if r.route.include_in_schema for m in r.methods}
    assert documented <= listed and len([r for r in routes if r.mutating]) >= 100


def test_every_mutating_route_is_protected_by_permissions():
    missing = [f"{sorted(r.methods)} {r.path}" for r in api_routes(create_app())
               if r.mutating and not reachable_actions(r.route.endpoint)]
    assert missing == [], f"PERMISSIONS で保護されていないルート: {missing}"


def test_every_api_route_requires_authentication(client):
    bare = {}
    for r in api_routes(client.app):
        if not r.path.startswith(API):
            continue
        path = re.sub(r"{\w+(?::\w+)?}", "x", r.path)
        for method in r.methods - {"HEAD", "OPTIONS"}:
            status = client.request(method, path, headers={"X-Actor": ""}).status_code
            if status != 401:
                bare[f"{method} {r.path}"] = status
    assert bare == {}


def test_non_api_routes_are_only_read():
    """/api/v1 の外には更新系のルートを置かない (health、ready と UI の静的ファイルだけ)。"""
    outside = [r.path for r in api_routes(create_app()) if not r.path.startswith(API)]
    assert outside and not [r.path for r in api_routes(create_app()) if not r.path.startswith(API) and r.mutating]


def test_detector_catches_an_unprotected_route():
    """検出器そのものの確認: 権限を判定しないルートを足すと検出できる。"""
    router = APIRouter()

    @router.post("/unprotected")
    def unprotected():
        return {}

    @router.post("/protected")
    def protected(principal: Principal = Depends(get_principal)):
        authz.require(None, principal, "audit.read")

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    found = {r.path: reachable_actions(r.route.endpoint) for r in api_routes(app)}
    assert found["/api/v1/unprotected"] == frozenset() and found["/api/v1/protected"] == {"audit.read"}


def test_every_permission_is_used_by_the_code():
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "edgf").rglob("*.py") if p.name != "authz.py")
    source += (ROOT / "edgf" / "authz.py").read_text(encoding="utf-8").split("def validate_permissions")[1]
    assert [a for a in authz.PERMISSIONS if f'"{a}"' not in source] == []


# ---------------------------------------------------------------- 権限表の総当たり
def _readme_table() -> dict[str, dict]:
    """README の権限表を読む。判定の実装から独立した、公開された仕様として比べる。"""
    table = {}
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| `([a-z_.]+)` \| .* \| (.*) \| (✓?) \|$", line)
        if not m or m.group(1) not in authz.PERMISSIONS:
            continue
        who = m.group(2).split("<br>")
        pick = lambda prefix: {x.strip() for w in who if w.startswith(prefix) for x in w[len(prefix):].split(",")}  # noqa: E731
        table[m.group(1)] = {
            "everyone": "認証済みの全員" in who,
            "roles": pick("ロール: "),
            "domain_roles": pick("ドメイン内ロール: "),
            "relations": {x.strip() for w in who if w.startswith("対象の ") for x in w[len("対象の "):].split(" / ")},
        }
    return table


def _expected(spec: dict, role: str, in_domain: bool, relation: str | None) -> bool:
    return (spec["everyone"] or role in spec["roles"] or (in_domain and role in spec["domain_roles"])
            or relation in spec["relations"])


def test_exhaustive_permission_matrix_matches_the_table(client):
    """PERMISSIONS × 6 ロール × ドメインの内外 × 関係のすべてで、許可と拒否が権限表どおりで、拒否は監査に残る。"""
    table = _readme_table()
    assert set(table) == set(authz.PERMISSIONS)
    wrong, denied = [], 0
    with SessionLocal() as db:
        for action, role, in_domain, relation in itertools.product(sorted(table), ROLES, (True, False), RELATIONS):
            subject = "actor"
            asset = DataAsset(id=str(uuid.uuid4()), qualified_name="x.y", name="y", domain="sales",
                              **{r: subject if r == relation else "other" for r in authz.ASSET_RELATIONS})
            principal = Principal(subject, normalize_roles(role), "internal", frozenset({"sales"} if in_domain else {"hr"}), "dev")
            expected = _expected(table[action], role, in_domain, relation)
            before = db.query(AuditEvent).count() if not expected else 0
            try:
                authz.require(db, principal, action, asset=asset, entity_id="target",
                              relations=[relation] if relation in ("assignee", "requester") else [])
                allowed = True
            except ProblemError as e:
                allowed = False
                assert e.status_code == 403
            if allowed != expected:
                wrong.append((action, role, in_domain, relation, allowed))
            if not allowed:
                denied += 1
                last = db.query(AuditEvent).order_by(AuditEvent.seq.desc()).first()
                if db.query(AuditEvent).count() != before + 1 or last.action != "authz.denied" or last.payload["action"] != action:
                    wrong.append((action, role, in_domain, relation, "not audited"))
    assert wrong == []
    assert denied > 1000
    with SessionLocal() as db:
        assert verify_chain(db)["valid"] is True


def test_custodian_holds_no_approval_permission():
    """§4 Business Ownership を表の側からも確認する。"""
    approvals = [a for a, r in authz.PERMISSIONS.items() if r.approval]
    assert len(approvals) > 20
    for action in approvals:
        rule = authz.PERMISSIONS[action]
        assert CUSTODIAN not in rule.roles | rule.domain_roles and "custodian" not in rule.relations, action


# ---------------------------------------------------------------- ルートに対する総当たり
# 認証済みなら誰でも呼べるルート (関係のない利用者でも 2xx があり得る)
OPEN_ACTIONS = frozenset(a for a, r in authz.PERMISSIONS.items() if r.authenticated)


def test_unrelated_user_cannot_mutate_through_any_route(client):
    """無関係の利用者で全更新系ルートを呼ぶ。実在する対象と形の正しい本文を渡し、認可まで到達させる。"""
    world = build_world(client)
    openapi = client.app.openapi()
    leaked, not_audited, reached = [], [], set()
    for r in api_routes(client.app):
        if not r.mutating or not r.path.startswith(API):
            continue
        actions = reachable_actions(r.route.endpoint)
        for method, path in candidates(r, world):
            body = fill_body(openapi, r.path, method, world)
            response = client.request(method, path, headers=ANALYST, json=body)
            assert response.status_code < 500, f"{method} {path}: {response.text}"
            if response.status_code < 300 and not actions & OPEN_ACTIONS:
                leaked.append(f"{method} {path} -> {response.status_code}")
            if response.status_code == 403:
                reached.add(r.path)
                rid = response.headers["x-request-id"]
                events = client.get(f"{API}/audit/events", headers=ADMIN_H, params={"request_id": rid, "action": "authz.denied"}).json()
                if not events:
                    not_audited.append(f"{method} {path}")
    assert leaked == [] and not_audited == []
    # 検証エラーや 404 で手前で止まるだけのテストにならないよう、十分な数のルートで認可に到達していることを確認する
    assert len(reached) >= 60, sorted(reached)


@pytest.mark.parametrize("role", ["admin", "committee"])
def test_privileged_roles_read_audit_others_do_not(client, role):
    assert client.get(f"{API}/audit/events", headers={"X-Actor": "x", "X-Roles": role}).status_code == 200
    assert client.get(f"{API}/audit/events", headers={"X-Actor": "x", "X-Roles": "owner,steward,custodian", "X-Domains": "*"}).status_code == 403
