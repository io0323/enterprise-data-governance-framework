"""Phase 15c: Output Port の到達の報告と確認、MESH-007."""
from __future__ import annotations

import socket
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from edgf.config import Settings
from edgf.db import SessionLocal
from edgf.models import ReachabilityReport, utcnow
from edgf.services import mesh_reachability, probe
from tests.mesh_helpers import ADMIN, API, COMMITTEE, MESH, U, add_port, domain, port_asset, product, shippable

ON = Settings(auth_mode="dev", env="test", probe_enabled=True, probe_allowed_hosts=("data.example.com", "*.cdn.example.com"))


def _datsis_failed(client, pid):
    return {r["policy_id"]: r["message"] for r in client.get(f"{MESH}/products/{pid}/datsis", headers=ADMIN).json()["results"] if r["status"] == "fail"}


@pytest.fixture()
def port(client):
    domain(client)
    p = product(client)
    a = port_asset(client)
    return client, add_port(client, p["id"], a["id"], reachable=False), p


# ---------------------------------------------------------------- 報告
def test_custodian_reports_and_mesh_007_follows(port):
    client, pt, p = port
    assert _datsis_failed(client, p["id"])["MESH-007"].endswith("到達の報告がありません")
    r = client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=U("cust"), json={"reachable": False, "detail": "connect_timeout"})
    assert r.status_code == 201 and r.json()["method"] == "push" and r.json()["reporter"] == "dev:cust"
    assert "到達できません (connect_timeout)" in _datsis_failed(client, p["id"])["MESH-007"]
    assert client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=U("cust"), json={"reachable": True}).status_code == 201
    assert "MESH-007" not in _datsis_failed(client, p["id"])
    history = client.get(f"{MESH}/ports/{pt['id']}/reachability", headers=ADMIN).json()
    assert [h["reachable"] for h in history] == [True, False]


def test_stale_report_fails_and_future_time_is_clamped(port):
    client, pt, p = port
    old = (utcnow() - timedelta(hours=25)).isoformat()
    assert client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=U("cust"), json={"reachable": True, "observed_at": old}).status_code == 201
    assert "24 時間より古い" in _datsis_failed(client, p["id"])["MESH-007"]
    future = (utcnow() + timedelta(days=30)).isoformat()
    r = client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=U("cust"), json={"reachable": True, "observed_at": future})
    assert r.status_code == 201 and r.json()["observed_at"] <= utcnow().isoformat()
    assert "MESH-007" not in _datsis_failed(client, p["id"])


@pytest.mark.parametrize("who", [U("someone"), U("asset-owner"), U("x", "custodian", "sales"), ADMIN, COMMITTEE])
def test_only_the_port_custodian_reports(port, who):
    client, pt, _ = port
    assert client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=who, json={"reachable": True}).status_code == 403


@pytest.mark.parametrize("body", [{"reachable": True, "detail": "user@example.com"}, {"reachable": True, "detail": "x" * 201},
                                  {"reachable": True, "body": "<html>"}, {}])
def test_report_validation(port, body):
    client, pt, _ = port
    assert client.post(f"{MESH}/ports/{pt['id']}/reachability", headers=U("cust"), json=body).status_code == 422


def test_unreadable_port_history_and_missing_port(client):
    domain(client)
    p = product(client)
    a = port_asset(client, sensitivity="restricted")
    pt = add_port(client, p["id"], a["id"], reachable=False)
    assert client.get(f"{MESH}/ports/{pt['id']}/reachability", headers=U("reader")).status_code == 403
    assert client.get(f"{MESH}/ports/none/reachability", headers=ADMIN).status_code == 404
    assert client.post(f"{MESH}/ports/none/reachability", headers=U("cust"), json={"reachable": True}).status_code == 404


def test_shippable_product_is_publishable_with_a_report(client):
    ctx = shippable(client)
    assert "MESH-007" not in _datsis_failed(client, ctx["pid"])


def test_mesh_007_is_not_required_when_undefined(client, monkeypatch):
    """既存の環境では Policy 変更の承認を経て有効になる。承認前 (定義が無い) は公開を止めない。"""
    from edgf.services import mesh_datsis
    assert "MESH-007" not in mesh_datsis.REQUIRED_POLICIES


def test_overview_uses_batched_reports(client):
    domain(client)
    ids = []
    for i in range(3):
        p = product(client, f"p{i}")
        add_port(client, p["id"], port_asset(client, f"sales.p{i}")["id"], reachable=(i != 1))
        ids.append(p["id"])
    rows = {r["id"]: r for r in client.get(f"{MESH}/products/overview", headers=COMMITTEE).json()}
    assert ["MESH-007" in rows[i]["datsis_failed"] for i in ids] == [False, True, False]


# ---------------------------------------------------------------- Control Plane からの確認
def test_probe_is_disabled_by_default_and_never_connects(port, monkeypatch):
    client, pt, _ = port

    def forbidden(*args, **kwargs):
        raise AssertionError("外部に接続してはいけない")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    r = client.post(f"{MESH}/ports/{pt['id']}/probe", headers=ADMIN)
    assert r.status_code == 409 and r.json()["type"] == "urn:edgf:problem:probe-disabled"
    from edgf.jobs import run_job
    assert run_job("reachability_probe", force=True)["result"] == {"probed": 0, "refused": 0}
    assert client.get(f"{MESH}/probe", headers=ADMIN).json()["enabled"] is False
    assert client.get(f"{MESH}/probe", headers=COMMITTEE).status_code == 403
    assert client.post(f"{MESH}/ports/{pt['id']}/probe", headers=COMMITTEE).status_code == 403


def test_enabled_without_allowlist_refuses_to_start():
    from edgf.main import create_app
    with pytest.raises(probe.ProbeConfigError):
        create_app(Settings(auth_mode="dev", env="test", probe_enabled=True))


class Recorder:
    def __init__(self, addresses, status=200):
        self.addresses = list(addresses)
        self.status = status
        self.resolved, self.fetched = [], []

    def resolve(self, host, port):
        self.resolved.append(host)
        return self.addresses.pop(0) if self.addresses and isinstance(self.addresses[0], list) else self.addresses

    def fetch(self, ip, host, port, path, timeout):
        self.fetched.append((ip, host, port, path, timeout))
        return self.status


def _probe(uri, recorder, settings=ON):
    return probe.probe(uri, settings, resolver=recorder.resolve, fetcher=recorder.fetch)


def test_allowed_public_host_is_probed_once_on_the_checked_address():
    r = Recorder(["93.184.216.34"], status=401)
    result = _probe("https://data.example.com/orders?x=1", r)
    assert result.reachable is True and result.detail == "http_401"
    assert r.fetched == [("93.184.216.34", "data.example.com", 443, "/orders?x=1", 5.0)] and r.resolved == ["data.example.com"]
    assert _probe("https://a.cdn.example.com:8443/", Recorder(["93.184.216.35"])).reachable is True


@pytest.mark.parametrize("uri, code", [
    ("http://data.example.com/", "https_required"), ("ftp://data.example.com/", "https_required"), ("https:///x", "https_required"),
    ("https://user:pw@data.example.com/", "credentials_in_uri"), ("https://evil.example.org/", "host_not_allowed"),
    ("https://data.example.com.evil.org/", "host_not_allowed"), ("https://cdn.example.com/", "host_not_allowed"), (None, "https_required"),
])
def test_refused_before_any_connection(uri, code):
    r = Recorder(["93.184.216.34"])
    with pytest.raises(probe.ProbeRefused) as e:
        _probe(uri, r)
    assert e.value.code == code and r.resolved == [] and r.fetched == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.5"], ["192.168.1.2"], ["172.16.0.1"], ["169.254.169.254"], ["100.64.0.1"],
                                       ["::1"], ["fe80::1"], ["fc00::1"], ["::ffff:10.0.0.1"], ["0.0.0.0"], ["224.0.0.1"],
                                       ["93.184.216.34", "10.0.0.5"]])
def test_private_loopback_and_link_local_addresses_are_refused(addresses):
    r = Recorder(addresses)
    with pytest.raises(probe.ProbeRefused) as e:
        _probe("https://data.example.com/", r)
    assert e.value.code == "address_not_public" and r.fetched == []


def test_dns_rebinding_cannot_redirect_the_connection():
    """1 回目の解決は公開のアドレス、2 回目は内部のアドレスを返す攻撃。解決は 1 回だけで、確認したアドレスに接続する。"""
    r = Recorder([["93.184.216.34"], ["127.0.0.1"]])
    assert _probe("https://data.example.com/", r).reachable is True
    assert r.resolved == ["data.example.com"] and r.fetched[0][0] == "93.184.216.34"


def test_redirect_is_not_followed():
    r = Recorder(["93.184.216.34"], status=302)
    result = _probe("https://data.example.com/", r)
    assert result.detail == "http_302" and len(r.fetched) == 1


def test_failures_are_reported_not_raised():
    def broken(*args):
        raise OSError("refused")
    assert probe.probe("https://data.example.com/", ON, resolver=broken, fetcher=lambda *a: 200) == probe.ProbeResult(False, "dns_failed")
    assert probe.probe("https://data.example.com/", ON, resolver=lambda h, p: [], fetcher=lambda *a: 200).detail == "dns_failed"
    result = probe.probe("https://data.example.com/", ON, resolver=lambda h, p: ["93.184.216.34"], fetcher=broken)
    assert result == probe.ProbeResult(False, "connect_failed:OSError")
    assert _probe("https://data.example.com/", Recorder(["93.184.216.34"], status=503)).reachable is False
    with pytest.raises(probe.ProbeRefused):
        probe.probe("https://data.example.com/", replace(ON, probe_enabled=False))


def test_enabled_probe_job_records_results(client, monkeypatch):
    domain(client)
    p = product(client)
    ok = add_port(client, p["id"], port_asset(client, "sales.a")["id"], address_uri="https://data.example.com/a", reachable=False)
    bad = add_port(client, p["id"], port_asset(client, "sales.b")["id"], name="b", address_uri="https://internal.local/b", reachable=False)
    with SessionLocal() as db:
        from edgf.models import DataProduct
        db.get(DataProduct, p["id"]).status = "published"
        db.commit()
    monkeypatch.setattr(mesh_reachability, "settings", ON)
    monkeypatch.setattr(probe, "settings", ON)
    monkeypatch.setattr(probe, "system_resolver", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(probe, "https_fetch", lambda *a: 204)
    from edgf.services.probe import probe as real_probe
    monkeypatch.setattr(probe, "probe", lambda uri, s=None: real_probe(uri, ON, resolver=lambda h, pt: ["93.184.216.34"], fetcher=lambda *a: 204))
    with SessionLocal() as db:
        assert mesh_reachability.probe_all(db) == {"probed": 1, "refused": 1}
        db.commit()
        reports = {r.port_id: r for r in db.query(ReachabilityReport).all()}
    assert reports[ok["id"]].reachable is True and reports[ok["id"]].method == "probe"
    assert reports[bad["id"]].reachable is False and reports[bad["id"]].detail == "refused:host_not_allowed"


def test_manual_probe_when_enabled(client, monkeypatch):
    from edgf.api import mesh as mesh_api
    from edgf.services.probe import probe as real_probe
    domain(client)
    p = product(client)
    pt = add_port(client, p["id"], port_asset(client)["id"], address_uri="https://data.example.com/a", reachable=False)
    monkeypatch.setattr(mesh_api, "settings", ON)
    monkeypatch.setattr(probe, "probe", lambda uri, s=None: real_probe(uri, ON, resolver=lambda h, x: ["93.184.216.34"], fetcher=lambda *a: 200))
    r = client.post(f"{MESH}/ports/{pt['id']}/probe", headers=ADMIN)
    assert r.status_code == 200 and r.json()["reachable"] is True and r.json()["detail"] == "http_200"
    monkeypatch.setattr(probe, "probe", lambda uri, s=None: real_probe(uri, ON, resolver=lambda h, x: ["10.0.0.1"], fetcher=lambda *a: 200))
    assert client.post(f"{MESH}/ports/{pt['id']}/probe", headers=ADMIN).json()["detail"] == "refused:address_not_public"
