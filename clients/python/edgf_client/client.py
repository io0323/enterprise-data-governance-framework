"""Governance Control Plane のクライアント.

- 認証: OAuth 2.0 Client Credentials (ClientCredentials)。401 を受けたらトークンを取り直して 1 回だけ送り直す
- 再試行: 冪等な操作だけ。送信系には Idempotency-Key を付け、同じ鍵で再送するので二重に登録されない
- エラー: Problem Details を edgf_client.errors の型付きの例外にする
"""
from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx

from edgf_client.auth import TokenProvider
from edgf_client.errors import TransportError, problem_from_response

API = "/api/v1"
RETRY_STATUSES = frozenset({429, 502, 503, 504})


class EdgfClient:
    def __init__(self, base_url: str, credentials: TokenProvider, *, timeout: float = 30.0, max_attempts: int = 3,
                 backoff: float = 0.5, max_backoff: float = 10.0, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.max_attempts = max(1, max_attempts)
        self.backoff = backoff
        self.max_backoff = max_backoff
        self._http = http or httpx.Client(timeout=timeout)
        self._sleep = sleep

    # ------------------------------------------------------------ 共通
    def request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, idempotent: bool | None = None,
                idempotency_key: str | None = None) -> httpx.Response:
        """1 つの要求を送る。

        再試行するのは、GET などの安全な操作、冪等と宣言した操作 (idempotent=True)、Idempotency-Key を付けた操作だけ。
        それ以外の送信は、通信が失敗しても送り直さない (二重に処理されるおそれがあるため)。
        """
        method = method.upper()
        retryable = idempotent if idempotent is not None else method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")
        retryable = retryable or idempotency_key is not None
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempt, refreshed = 0, False
        while True:
            attempt += 1
            try:
                response = self._http.request(method, url, json=json, params=params,
                                              headers={**headers, "Authorization": f"Bearer {self.credentials.token()}"})
            except httpx.TransportError as e:
                if not retryable or attempt >= self.max_attempts:
                    raise TransportError(f"{method} {path}: Control Plane に届きません ({type(e).__name__})") from e
                self._sleep(self._delay(attempt, None))
                continue
            if response.status_code == 401 and not refreshed:  # 期限の前に失効したトークン
                refreshed = True
                self.credentials.invalidate()
                attempt -= 1
                continue
            in_progress = response.status_code == 409 and idempotency_key is not None and "idempotency-in-progress" in response.text
            if (response.status_code in RETRY_STATUSES or in_progress) and retryable and attempt < self.max_attempts:
                self._sleep(self._delay(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code >= 400:
                raise problem_from_response(response)
            return response

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), self.max_backoff)
        return min(self.backoff * 2 ** (attempt - 1), self.max_backoff) * (0.5 + random.random() / 2)  # noqa: S311  # nosec B311 - 間隔をずらすだけ

    def _send(self, path: str, body: Any, idempotency_key: str | None = None) -> Any:
        """状態を変える送信。Idempotency-Key を付ける (再試行でも同じ鍵を使う)。"""
        response = self.request("POST", path, json=body, idempotency_key=idempotency_key or str(uuid.uuid4()))
        return response.json() if response.content else None

    def _query(self, path: str, body: Any) -> Any:
        """状態を変えない POST (判定や照合)。冪等なので再試行する。"""
        return self.request("POST", path, json=body, idempotent=True).json()

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    # ------------------------------------------------------------ 品質、リネージ、更新の通知
    def submit_quality_run(self, asset_id: str, records: Iterable[Mapping[str, Any]], *, idempotency_key: str | None = None) -> dict:
        """品質検査の対象のレコードを送る。Control Plane は評価だけを行い、レコードは保存しない。"""
        return self._send(f"{API}/quality/assets/{asset_id}/runs", {"records": list(records)}, idempotency_key)

    def send_openlineage(self, event: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict:
        return self._send(f"{API}/lineage/openlineage", dict(event), idempotency_key)

    def notify_refreshed(self, asset_id: str, *, idempotency_key: str | None = None) -> dict:
        return self._send(f"{API}/assets/{asset_id}/refreshed", None, idempotency_key)

    # ------------------------------------------------------------ Right to Erasure
    def erasure_tasks(self, status: str | None = None, *, limit: int = 100) -> list[dict]:
        return self._get(f"{API}/privacy/erasure-tasks", {"limit": limit, **({"status": status} if status else {})})

    def ack_erasure_task(self, task_id: str, *, idempotency_key: str | None = None) -> dict:
        return self._send(f"{API}/privacy/erasure-tasks/{task_id}/ack", None, idempotency_key)

    def complete_erasure_task(self, task_id: str, *, outcome: str = "completed", records_deleted: int = 0, evidence_hash: str | None = None,
                              note: str = "", idempotency_key: str | None = None) -> dict:
        body = {"outcome": outcome, "records_deleted": records_deleted, "evidence_hash": evidence_hash, "note": note}
        return self._send(f"{API}/privacy/erasure-tasks/{task_id}/complete", body, idempotency_key)

    # ------------------------------------------------------------ Data Contract (CI)
    def check_contract(self, contract_id: str, proposal: Mapping[str, Any]) -> dict:
        """Contract の案の互換性の判定。公開はしない (CI で使う)。"""
        return self._query(f"{API}/mesh/contracts/{contract_id}/check", dict(proposal))

    # ------------------------------------------------------------ MDM
    def mdm_match(self, entity_type: str, record: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]] = (), *,
                  idempotency_key: str | None = None) -> dict:
        return self._send(f"{API}/mdm/{entity_type}/match", {"record": dict(record), "candidates": list(candidates)}, idempotency_key)

    def mdm_compose(self, entity_type: str, golden_id: str, records: Iterable[Mapping[str, Any]], overrides: Mapping[str, str] | None = None, *,
                    idempotency_key: str | None = None) -> dict:
        body = {"records": list(records), "overrides": dict(overrides or {})}
        return self._send(f"{API}/mdm/{entity_type}/golden-records/{golden_id}/compose", body, idempotency_key)

    def mdm_verify(self, entity_type: str, golden_id: str, attributes: Mapping[str, Any], version: int | None = None) -> dict:
        """Data Plane が持つ値の内容ハッシュを照合する。値は照合のあと破棄される。"""
        return self._query(f"{API}/mdm/{entity_type}/golden-records/{golden_id}/verify", {"attributes": dict(attributes), "version": version})

    # ------------------------------------------------------------ RAG
    def rag_authorize(self, corpus_asset_id: str, user: Mapping[str, Any], hit_asset_ids: Iterable[str]) -> dict:
        body = {"corpus_asset_id": corpus_asset_id, "user": dict(user), "hit_asset_ids": list(hit_asset_ids)}
        return self._query(f"{API}/ai/rag/authorize", body)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EdgfClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
