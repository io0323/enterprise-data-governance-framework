"""OAuth 2.0 Client Credentials によるトークンの取得と更新."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from edgf_client.errors import TokenError


class TokenProvider(Protocol):
    def token(self) -> str: ...

    def invalidate(self) -> None: ...


class StaticToken:
    """固定のトークン (検証用)。更新はしない。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def token(self) -> str:
        return self.value

    def invalidate(self) -> None:
        pass


class ClientCredentials:
    """トークンをキャッシュし、有効期限の refresh_margin 秒前に更新する。

    client_secret はメモリにだけ持つ。ログや例外のメッセージには出さない。
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str, *, scope: str | None = None, audience: str | None = None,
                 refresh_margin: float = 60.0, timeout: float = 10.0, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self._secret = client_secret
        self.scope = scope
        self.audience = audience
        self.refresh_margin = refresh_margin
        self._http = http or httpx.Client(timeout=timeout)
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.fetch_count = 0

    def __repr__(self) -> str:
        return f"ClientCredentials(token_url={self.token_url!r}, client_id={self.client_id!r})"

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - self.refresh_margin:
                self._fetch()
            return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _fetch(self) -> None:
        form = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret}
        if self.scope:
            form["scope"] = self.scope
        if self.audience:
            form["audience"] = self.audience
        try:
            response = self._http.post(self.token_url, data=form)
        except httpx.HTTPError as e:
            raise TokenError(f"トークンエンドポイントに接続できません ({type(e).__name__})") from e
        if response.status_code != 200:
            raise TokenError(f"トークンを取得できません (HTTP {response.status_code})")
        body = response.json()
        if not body.get("access_token"):
            raise TokenError("トークンの応答に access_token がありません")
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in") or 300)
        self.fetch_count += 1
