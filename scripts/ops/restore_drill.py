#!/usr/bin/env python
"""バックアップと復元の訓練: pg_dump → 別の DB に復元 → /audit/verify が valid でアンカーと一致することを確認する.

    EDGF_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/source python scripts/ops/restore_drill.py

確認すること:
1. 監査イベントとアンカーを作ってバックアップし、別の DB に復元すると valid で、アンカーが一致する
2. 復元した DB の業務データの件数が元と同じ
3. バックアップのあとにアンカーを追加すると、古いバックアップの復元は tail_truncated になる (失われた範囲が分かる)

pg_dump などをコンテナの中で実行するときは EDGF_DRILL_PG_PREFIX="docker compose exec -T db" を指定する。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TABLES = ("audit_events", "audit_anchors", "domains", "data_assets", "outbox_events")


def pg(url: str, command: list[str], stdin: bytes | None = None) -> bytes:
    """pg_dump などの実行。接続情報は引数に置かず、環境変数で渡す (プロセス一覧に出さない)。"""
    parts = urlsplit(url)
    prefix = shlex.split(os.environ.get("EDGF_DRILL_PG_PREFIX", ""))
    env = {**os.environ, "PGPASSWORD": parts.password or ""}
    connection = ["-U", parts.username or ""]
    if not prefix:
        connection += ["-h", parts.hostname or "localhost", "-p", str(parts.port or 5432)]
    done = subprocess.run([*prefix, command[0], *connection, *command[1:]], input=stdin, env=env, cwd=ROOT, capture_output=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"{command[0]} failed: {done.stderr.decode(errors='replace')[:2000]}")
    return done.stdout


def in_app(url: str, anchor_file: Path, key: str, step: str) -> dict:
    """本番と同じくプロセスを分けて実行する (接続先は起動時に決まるため)。"""
    env = {**os.environ, "EDGF_DATABASE_URL": url, "EDGF_AUTH_MODE": "dev", "EDGF_ENV": "test", "EDGF_LOG_LEVEL": "ERROR", "EDGF_ANCHOR_ENABLED": "true",
           "EDGF_ANCHOR_SIGNING_KEY": key, "EDGF_ANCHOR_FILE": str(anchor_file), "EDGF_ANCHOR_OUTBOX": "true", "PYTHONPATH": str(ROOT)}
    done = subprocess.run([sys.executable, __file__, "--step", step], env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"step {step} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def step(name: str) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from edgf import db, migrate
    from edgf.jobs import run_job
    from edgf.main import create_app

    admin = {"X-Actor": "drill", "X-Roles": "admin"}
    committee = {"X-Actor": "auditor", "X-Roles": "committee"}
    if name == "seed":
        migrate.upgrade(db.engine)
    with TestClient(create_app()) as client:
        if name in ("seed", "more"):
            label = secrets.token_hex(4)
            for i in range(5):
                r = client.post("/api/v1/domains", headers=admin, json={"name": f"drill-{label}-{i}"})
                assert r.status_code == 201, r.text
            job = run_job("audit_anchor")
            assert job["status"] == "succeeded" and job["result"]["anchored"] == 1 and job["result"]["sink_failures"] == [], job
        verify = client.get("/api/v1/audit/verify", headers=committee).json()
    with db.engine.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}  # nosec B608 - 表名は固定の定数
    print(json.dumps({"verify": verify, "counts": counts}, default=str))


def main() -> int:
    source = os.environ["EDGF_DATABASE_URL"]
    parts = urlsplit(source)
    assert parts.scheme.startswith("postgresql"), "PostgreSQL の接続先を指定してください"
    base = parts.path.lstrip("/")
    restored, stale = f"{base}_restored", f"{base}_stale"
    key = base64.b64encode(secrets.token_bytes(32)).decode()

    def url_of(name: str) -> str:
        return parts._replace(path=f"/{name}").geturl()

    with tempfile.TemporaryDirectory() as tmp:
        anchors = Path(tmp) / "anchors.jsonl"
        for name in (restored, stale):
            pg(source, ["dropdb", "--if-exists", name])
        before = in_app(source, anchors, key, "seed")
        assert before["verify"]["valid"] is True and before["verify"]["anchor"]["status"] == "ok", before
        dump = pg(source, ["pg_dump", "-Fc", "-d", base])
        print(f"backup: {len(dump)} bytes, {before['counts']}")

        pg(source, ["createdb", restored])
        pg(source, ["pg_restore", "--no-owner", "--exit-on-error", "-d", restored], stdin=dump)
        after = in_app(url_of(restored), anchors, key, "check")
        assert after["verify"]["valid"] is True, after
        assert after["verify"]["anchor"]["status"] == "ok", after
        assert after["verify"]["anchor"]["latest"] == before["verify"]["anchor"]["latest"], after
        assert after["counts"] == before["counts"], (before["counts"], after["counts"])
        print(f"restored: valid, anchor seq {after['verify']['anchor']['latest']['seq']} matches")

        # バックアップのあとに進んだ分は失われる。ファイルに残ったアンカーで範囲が分かる
        newer = in_app(source, anchors, key, "more")
        assert newer["verify"]["anchor"]["latest"]["seq"] > before["verify"]["anchor"]["latest"]["seq"], newer
        pg(source, ["createdb", stale])
        pg(source, ["pg_restore", "--no-owner", "--exit-on-error", "-d", stale], stdin=dump)
        old = in_app(url_of(stale), anchors, key, "check")
        assert old["verify"]["valid"] is False and old["verify"]["anchor"]["status"] == "tail_truncated", old
        print(f"stale backup detected: head {old['verify']['anchor']['head_seq']} < anchor {old['verify']['anchor']['latest']['seq']}")
        for name in (restored, stale):
            pg(source, ["dropdb", "--if-exists", name])
    print("restore drill OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--step":
        step(sys.argv[2])
    else:
        sys.exit(main())
