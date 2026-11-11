"""Data Plane 連携の E2E (サーバを別プロセスで起動し、SDK で呼ぶ)。EDGF_DATA_PLANE_E2E=1 のときだけ集める."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

if not os.environ.get("EDGF_DATA_PLANE_E2E"):
    collect_ignore_glob = ["test_*.py"]

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
DATA_PLANE = "dp-custodian"  # Data Plane のサービスの主体。対象の資産の Custodian にする
SECRET = "dp-secret-value-0001"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def idp():
    from tests.e2e.fake_idp import FakeIdp
    server = FakeIdp()
    server.clients[DATA_PLANE] = {"secret": SECRET, "roles": ["custodian"]}
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def server(idp, tmp_path_factory):
    db = tmp_path_factory.mktemp("dp") / "dp.db"
    port = _free_port()
    env = {**os.environ, "EDGF_DATABASE_URL": f"sqlite:///{db}", "EDGF_ENV": "test", "EDGF_AUTH_MODE": "jwt", "EDGF_OIDC_ISSUER": idp.issuer,
           "EDGF_OIDC_AUDIENCE": "edgf-control-plane", "EDGF_OIDC_JWKS_URL": f"{idp.issuer}/jwks", "EDGF_LOG_LEVEL": "WARNING"}
    subprocess.run([str(Path(sys.executable).parent / "alembic"), "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    log = (tmp_path_factory.mktemp("dp-log") / "server.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "edgf.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.fail("server did not start")
    yield {"url": url, "env": env, "log": Path(log.name), "db": db}
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture(scope="session")
def as_user(idp, server):
    """準備の操作に使う利用者 (偽の IdP が発行するトークン)。"""
    def make(sub, roles, domains=()):
        token = idp.access_token({"sub": sub, "roles": list(roles), "domains": list(domains)})
        return httpx.Client(base_url=server["url"] + API, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return make


@pytest.fixture(scope="session")
def sdk(idp, server):
    from edgf_client import ClientCredentials, EdgfClient
    credentials = ClientCredentials(idp.token_endpoint, DATA_PLANE, SECRET)
    with EdgfClient(server["url"], credentials) as client:
        yield client
