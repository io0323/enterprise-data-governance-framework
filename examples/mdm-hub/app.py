"""MDM Hub の参照実装 (Data Plane の側).

Golden Record の値は、この Hub が自分の SQLite に保持する。Control Plane (edgf) には値を渡して判定だけを受け、
永続化するのは由来・内容ハッシュ・版だけ (§8、§18.2)。SDK (edgf-client) を使う最小の例で、本番向けの機能
(候補の絞り込み、同時実行の制御、配信) は持たない。

    EDGF_URL=... EDGF_TOKEN_URL=... EDGF_CLIENT_ID=... EDGF_CLIENT_SECRET=... MDM_HUB_DB=hub.db uvicorn app:app

流れ:
1. POST /records: ソースレコードを受け取り、match → compose → verify を行って値を保存する
2. POST /erasure/run: Control Plane から消去タスクを取得し、ack → 値の削除 → 完了の報告を行う
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from edgf_client import ClientCredentials, EdgfClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS golden (id TEXT PRIMARY KEY, attributes TEXT NOT NULL, content_hash TEXT, version INTEGER, verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS source (source_system TEXT NOT NULL, key TEXT NOT NULL, golden_id TEXT, attributes TEXT NOT NULL,
                                   PRIMARY KEY (source_system, key));
"""


@dataclass(frozen=True)
class HubSettings:
    database: str
    entity_type: str = "party"

    @classmethod
    def from_env(cls) -> HubSettings:
        return cls(database=os.environ.get("MDM_HUB_DB", "mdm-hub.db"), entity_type=os.environ.get("MDM_ENTITY", "party"))


class SourceRecord(BaseModel):
    source_system: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=1, max_length=512)
    attributes: dict[str, Any]


class Hub:
    def __init__(self, settings: HubSettings, client: EdgfClient) -> None:
        self.settings = settings
        self.client = client
        self.db = sqlite3.connect(settings.database, check_same_thread=False)
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ 登録
    def _candidates(self) -> list[dict[str, Any]]:
        """比較の候補。参照実装なので全件を渡す (本番ではブロッキングで絞り込む。Control Plane の上限は 500 件)。"""
        return [{"golden_id": gid, "attributes": json.loads(attrs)} for gid, attrs in self.db.execute("SELECT id, attributes FROM golden")][:500]

    def _sources_of(self, golden_id: str) -> list[dict[str, Any]]:
        return [{"source_system": s, "key": k, "attributes": json.loads(a)}
                for s, k, a in self.db.execute("SELECT source_system, key, attributes FROM source WHERE golden_id = ?", (golden_id,))]

    def register(self, record: SourceRecord) -> dict[str, Any]:
        entity = self.settings.entity_type
        body = record.model_dump()
        matched = self.client.mdm_match(entity, body, self._candidates())
        golden_id = matched["golden_id"]
        self.db.execute("INSERT OR REPLACE INTO source (source_system, key, golden_id, attributes) VALUES (?, ?, ?, ?)",
                        (record.source_system, record.key, golden_id, json.dumps(record.attributes)))
        if matched["decision"] == "review" or golden_id is None:
            self.db.commit()
            return {"decision": "review", "golden_id": None, "verified": False}
        composed = self.client.mdm_compose(entity, golden_id, self._sources_of(golden_id))
        attributes = composed["attributes"]
        verified = self.client.mdm_verify(entity, golden_id, attributes, composed["version"])["match"]
        self.db.execute("INSERT OR REPLACE INTO golden (id, attributes, content_hash, version, verified) VALUES (?, ?, ?, ?, ?)",
                        (golden_id, json.dumps(attributes), composed["content_hash"], composed["version"], int(verified)))
        self.db.commit()
        return {"decision": matched["decision"], "golden_id": golden_id, "verified": verified, "version": composed["version"]}

    def golden(self, golden_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT attributes, version, verified FROM golden WHERE id = ?", (golden_id,)).fetchone()
        return None if row is None else {"golden_id": golden_id, "attributes": json.loads(row[0]), "version": row[1], "verified": bool(row[2])}

    # ------------------------------------------------------------ 消去
    def run_erasure(self) -> dict[str, int]:
        """発行済みの消去タスクを処理する。対象の Golden Record の値と、その構成ソースのレコードを物理削除する。"""
        completed = 0
        for task in self.client.erasure_tasks():
            if task["status"] not in ("pending", "acknowledged"):
                continue
            if task["status"] == "pending":
                self.client.ack_erasure_task(task["id"])
            golden_ids = task.get("golden_ids") or []
            deleted = 0
            for gid in golden_ids:
                deleted += self.db.execute("DELETE FROM source WHERE golden_id = ?", (gid,)).rowcount
                deleted += self.db.execute("DELETE FROM golden WHERE id = ?", (gid,)).rowcount
            self.db.commit()
            # 削除の証跡: 対象と件数のハッシュ (値は含めない)
            evidence = hashlib.sha256(json.dumps({"task": task["id"], "golden_ids": sorted(golden_ids), "deleted": deleted}).encode()).hexdigest()
            self.client.complete_erasure_task(task["id"], records_deleted=deleted, evidence_hash=evidence, note="mdm-hub")
            completed += 1
        return {"completed": completed}


def create_app(settings: HubSettings | None = None, client: EdgfClient | None = None) -> FastAPI:
    settings = settings or HubSettings.from_env()
    if client is None:
        credentials = ClientCredentials(os.environ["EDGF_TOKEN_URL"], os.environ["EDGF_CLIENT_ID"], os.environ["EDGF_CLIENT_SECRET"])
        client = EdgfClient(os.environ["EDGF_URL"], credentials)
    hub = Hub(settings, client)
    app = FastAPI(title="MDM Hub (reference)")

    @app.post("/records")
    def register(record: SourceRecord):
        return hub.register(record)

    @app.get("/golden/{golden_id}")
    def golden(golden_id: str):
        found = hub.golden(golden_id)
        if found is None:
            raise HTTPException(404, "golden record not found")
        return found

    @app.post("/erasure/run")
    def erasure():
        return hub.run_erasure()

    app.state.hub = hub
    return app


if os.environ.get("EDGF_URL"):  # pragma: no cover - uvicorn app:app で起動したとき
    app = create_app()
