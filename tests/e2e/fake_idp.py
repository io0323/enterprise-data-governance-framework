"""E2E テスト用の IdP の模擬 (標準ライブラリとローカルの署名鍵だけ).

OIDC のディスカバリ、認可エンドポイント (PKCE を記録して即座にリダイレクト)、トークンエンドポイント (PKCE と
機密クライアントの秘密を検証)、JWKS を提供する。本番のコードからは使わない。

Phase 17 から、ブラウザは IdP と通信しない。認可エンドポイントへの画面遷移だけがブラウザから来て、トークンの要求は
Control Plane (BFF) から来る。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

AUDIENCE = "edgf-control-plane"
CLIENT_ID = "edgf-ui"
CLIENT_SECRET = "e2e-client-secret"
KID = "e2e-key"


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.user: dict = {}
        self.auth_time_offset = 0  # 認証の時刻を過去にずらす (ステップアップの確認用)
        self.authorize_requests: list[dict] = []
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []
        # Client Credentials のクライアント (Data Plane のサービスの主体): client_id → {secret, roles, domains, clearance}
        self.clients: dict[str, dict] = {}
        self.client_token_lifetime = 3600
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/token"

    def sign(self, claims: dict, *, lifetime: int = 3600) -> str:
        now = int(time.time())
        body = {"iss": self.issuer, "aud": AUDIENCE, "iat": now, "exp": now + lifetime, **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": KID})

    def access_token(self, user: dict) -> str:
        return self.sign({"sub": user["sub"], "roles": user.get("roles", []), "domains": user.get("domains", []),
                          **({"clearance": user["clearance"]} if user.get("clearance") else {})})

    def _jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()

        def encode(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": encode(numbers.n), "e": encode(numbers.e)}]}

    def _handler(idp) -> type[BaseHTTPRequestHandler]:  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _json(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/.well-known/openid-configuration":
                    self._json(200, {"issuer": idp.issuer, "authorization_endpoint": f"{idp.issuer}/authorize", "token_endpoint": idp.token_endpoint,
                                     "jwks_uri": f"{idp.issuer}/jwks", "end_session_endpoint": f"{idp.issuer}/logout"})
                elif parts.path == "/jwks":
                    self._json(200, idp._jwks())
                elif parts.path == "/authorize":
                    code = f"code-{len(idp.codes)}-{int(time.time() * 1000)}"
                    idp.authorize_requests.append(dict(query))
                    # max_age を付けた要求は再認証したものとして、今の時刻を認証の時刻にする
                    auth_time = int(time.time()) - (0 if "max_age" in query else idp.auth_time_offset)
                    with idp.lock:
                        idp.codes[code] = {"user": dict(idp.user), "challenge": query.get("code_challenge"), "method": query.get("code_challenge_method"),
                                           "nonce": query.get("nonce"), "redirect_uri": query.get("redirect_uri"), "auth_time": auth_time}
                    self.send_response(302)
                    self.send_header("Location", f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}")
                    self.end_headers()
                elif parts.path == "/logout":
                    self.send_response(302)
                    self.send_header("Location", query["post_logout_redirect_uri"])
                    self.end_headers()
                else:
                    self._json(404, {})

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                form = {k: v[0] for k, v in parse_qs(body).items()}
                idp.token_requests.append(form)
                if form.get("grant_type") == "client_credentials":
                    registered = idp.clients.get(form.get("client_id", ""))
                    if registered is None or registered["secret"] != form.get("client_secret"):
                        self._json(401, {"error": "invalid_client"})
                        return
                    token = idp.sign({"sub": form["client_id"], "roles": registered.get("roles", []), "domains": registered.get("domains", [])},
                                     lifetime=idp.client_token_lifetime)
                    self._json(200, {"access_token": token, "token_type": "Bearer", "expires_in": idp.client_token_lifetime})
                    return
                grant = idp.codes.pop(form.get("code"), None)
                if grant is None or form.get("grant_type") != "authorization_code" or form.get("client_id") != CLIENT_ID \
                        or form.get("client_secret") != CLIENT_SECRET:
                    self._json(400, {"error": "invalid_grant"})
                    return
                digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if grant["method"] != "S256" or expected != grant["challenge"] or form.get("redirect_uri") != grant["redirect_uri"]:
                    self._json(400, {"error": "invalid_grant"})
                    return
                user = grant["user"]
                self._json(200, {"access_token": idp.access_token(user), "token_type": "Bearer", "expires_in": 3600,
                                 "id_token": idp.sign({"sub": user["sub"], "aud": CLIENT_ID, "nonce": grant["nonce"], "preferred_username": user["sub"],
                                                       "auth_time": grant["auth_time"]})})
        return Handler
