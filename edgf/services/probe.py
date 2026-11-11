"""§15 Addressable: Control Plane から Output Port に到達できるかを確かめる (既定は無効).

外部への接続は SSRF の入口になるので、有効にする場合は次をすべて守る。

- 許可するホストの一覧 (EDGF_PROBE_ALLOWED_HOSTS) を必須にし、一覧にないホストには接続しない
- https だけを使う。証明書はホスト名で検証する
- 名前を解決したあと、解決したアドレスのどれかがプライベート、ループバック、リンクローカル、予約などなら拒否する
- 確認に通ったアドレスにそのまま接続する (接続のときに解決し直さないので、DNS の再束縛で内部に向けられない)
- リダイレクトには従わない。タイムアウトを設ける
"""
from __future__ import annotations

import fnmatch
import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from edgf.config import Settings, settings

Resolver = Callable[[str, int], Sequence[str]]
Fetcher = Callable[[str, str, int, str, float], int]


class ProbeConfigError(RuntimeError):
    """有効なのに許可するホストが無い。起動を止める。"""


class ProbeRefused(Exception):
    """安全策により接続しない (理由は code)。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    detail: str  # 個人情報や応答の本文を含めない短い説明


def validate_probe_settings(s: Settings) -> None:
    if s.probe_enabled and not s.probe_allowed_hosts:
        raise ProbeConfigError("EDGF_PROBE_ENABLED=true には EDGF_PROBE_ALLOWED_HOSTS (許可するホストの一覧) が必要です (起動を中止します)")


def host_allowed(host: str, allowed: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(fnmatch.fnmatchcase(host, pattern.lower().rstrip(".")) for pattern in allowed)


def unsafe_address(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast
            or address.is_unspecified or not address.is_global)


def system_resolver(host: str, port: int) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """確認したアドレスに接続し、証明書の検証と SNI はホスト名で行う。"""

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:  # pragma: no cover - 実際のネットワーク
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def https_fetch(ip: str, host: str, port: int, path: str, timeout: float) -> int:  # pragma: no cover - 実際のネットワーク
    connection = _PinnedHTTPSConnection(host, ip, port, timeout)
    try:
        connection.request("HEAD", path or "/", headers={"User-Agent": "edgf-reachability-probe", "Host": host})
        return connection.getresponse().status  # 3xx も応答として扱い、Location には従わない
    finally:
        connection.close()


def probe(address_uri: str | None, s: Settings | None = None, *, resolver: Resolver = system_resolver, fetcher: Fetcher = https_fetch) -> ProbeResult:
    """安全策を順に適用してから 1 回だけ接続する。安全策で止めたものは ProbeRefused。"""
    s = s or settings
    if not s.probe_enabled:
        raise ProbeRefused("probe_disabled")
    parts = urlsplit(address_uri or "")
    if parts.scheme != "https" or not parts.hostname:
        raise ProbeRefused("https_required")
    if parts.username or parts.password:
        raise ProbeRefused("credentials_in_uri")
    host, port = parts.hostname, parts.port or 443
    if not host_allowed(host, s.probe_allowed_hosts):
        raise ProbeRefused("host_not_allowed")
    try:
        addresses = list(resolver(host, port))
    except OSError:
        return ProbeResult(False, "dns_failed")
    if not addresses:
        return ProbeResult(False, "dns_failed")
    if any(unsafe_address(ip) for ip in addresses):
        raise ProbeRefused("address_not_public")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        status = fetcher(addresses[0], host, port, path, s.probe_timeout_seconds)
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        return ProbeResult(False, f"connect_failed:{type(e).__name__}")
    return ProbeResult(status < 500, f"http_{status}")
