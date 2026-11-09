"""Phase 14c: ABAC の漏洩テスト. 全 GET ルートの本文とヘッダに、restricted 資産の目印が現れないこと."""
from __future__ import annotations

import json
from urllib.parse import quote, unquote

import pytest

from tests.security_helpers import api_routes
from tests.security_world import ADMIN, ANALYST, MARK, OUTSIDER, build_world, candidates

API = "/api/v1"


def _query_values(route, world) -> list[dict]:
    """クエリの指定なしと、機密の資産を指す指定の 2 通りで呼ぶ。パラメータの追加にも自動で追従する。"""
    hidden = world["hidden"]
    filled = {}
    for param in route.route.dependant.query_params:
        name = param.name
        if name in ("limit", "offset"):
            continue
        if name in world["values"]:
            filled[name] = world["values"][name][0]
        elif name == "q":
            filled[name] = MARK
        elif name in ("owner", "actor"):
            filled[name] = hidden["owner"]
        elif name == "domain":
            filled[name] = hidden["domain"]
        elif param.field_info.annotation in (bool, bool | None):
            filled[name] = "true"
    return [{}, filled] if filled else [{}]


def _strip_echo(text: str, supplied: list[str]) -> str:
    """呼び出し側が自分で渡した値のそのままの反射 (Problem の instance や Link) は漏洩ではないので除く。"""
    for value in supplied:
        text = text.replace(quote(value, safe=""), "").replace(value, "")
    return text


def _scan(client, headers, world) -> tuple[list[str], int, int]:
    leaks, calls, ok = [], 0, 0
    for route in api_routes(client.app):
        if route.mutating or "GET" not in route.methods:
            continue
        for method, path in candidates(route, world):
            for params in _query_values(route, world):
                response = client.request(method, path, headers=headers, params=params)
                calls += 1
                ok += response.status_code == 200
                assert response.status_code < 500, f"{path}: {response.text}"
                # パスの中の名前は、呼び出す側がもともと知っている値なので Problem の中での反射だけ除く
                supplied = [unquote(p) for p in path.split("/") if MARK in p] + [v for v in params.values() if isinstance(v, str) and MARK in v]
                body = response.text
                if response.status_code >= 400:
                    body = _strip_echo(body, supplied)
                if MARK in body.lower():
                    start = body.lower().index(MARK)
                    leaks.append(f"{path} {params} body: …{body[max(0, start - 60):start + 40]}…")
                header_text = _strip_echo(json.dumps(dict(response.headers), ensure_ascii=False), supplied)
                if MARK in header_text.lower():
                    leaks.append(f"{path} {params} headers")
    return leaks, calls, ok


@pytest.mark.parametrize("who", [ANALYST, OUTSIDER], ids=["consumer", "other-domain-owner"])
def test_marker_never_reaches_unauthorized_reader(client, who):
    world = build_world(client)
    leaks, calls, ok = _scan(client, who, world)
    assert leaks == [], "\n".join(leaks)
    assert calls >= 100 and ok >= 30, (calls, ok)  # 全部 403 や 404 で終わっていないことの確認


def test_marker_is_actually_visible_to_admin(client):
    """テストの前提の確認: 権限のある利用者には目印が見える (目印が埋まっていないのに合格にならない)。"""
    world = build_world(client)
    for path, expected in ((f"{API}/assets/{world['hidden']['id']}", f"{MARK}_column"), (f"{API}/lineage/{world['hidden']['qualified_name']}/upstream", f"{MARK}_expr"),
                           (f"{API}/issues", f"{MARK} の障害"), (f"{API}/mesh/contract-versions/{world['values']['version_id'][0]}", f"{MARK}_col")):
        response = client.get(path, headers=ADMIN)
        assert response.status_code == 200 and expected in response.text, path


def test_scanner_detects_a_leak(client, monkeypatch):
    """検査そのものの確認: 名前を伏せる処理を無効にすると検出する (14-0 の回帰を検出できる)。"""
    from edgf.api import lineage as lineage_api

    world = build_world(client)
    monkeypatch.setattr(lineage_api.NameRedactor, "label", lambda self, name: name)
    leaks, _, _ = _scan(client, ANALYST, world)
    assert any("/lineage/" in line for line in leaks)


def test_query_filler_follows_new_parameters(client):
    route = next(r for r in api_routes(client.app) if r.path == f"{API}/assets" and "GET" in r.methods)
    filled = _query_values(route, {"hidden": {"owner": "o", "domain": "d"}, "values": {}})[1]
    assert filled["q"] == MARK and filled["domain"] == "d" and filled["owner"] == "o"
