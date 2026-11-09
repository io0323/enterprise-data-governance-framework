"""§13 Evidence: 監査チェーンのアンカリング.

ハッシュチェーンだけでは、DB を書き換えられる者が末尾を削除したり、全行のハッシュを計算し直したりすると検知できない。
チェーンの先頭 (seq, hash, 時刻) に Ed25519 で署名し、DB の外 (WORM 前提のファイル、イベント、任意で RFC 3161 の TSA) に書き出す。
署名鍵を持たない者はアンカーを作れないので、外部のアンカーと照合すれば書き換えが分かる。

個人データを含まない。書き出すのは seq、ハッシュ、時刻、鍵 ID、署名だけ。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from edgf.config import Settings, settings
from edgf.models import AuditAnchor, AuditEvent, utcnow
from edgf.services import audit, outbox

log = logging.getLogger("edgf.audit")

FORMAT_VERSION = 1
SEED_BYTES = 32
CHUNK = 500
# verify の status のうち、改ざんや欠落を示すもの (valid を false にする)
BROKEN = frozenset({"tail_truncated", "mismatch", "signature_invalid", "file_tampered"})


class AnchorConfigError(RuntimeError):
    """アンカリングの設定の不備。起動を停止させる (fail fast)。"""


class AnchorSinkError(RuntimeError):
    """書き出しの失敗。例外のメッセージに外部の応答や URL を含めない。"""


# ---------------------------------------------------------------- 鍵
def load_private_key(encoded: str) -> Ed25519PrivateKey:
    try:
        seed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise AnchorConfigError("EDGF_ANCHOR_SIGNING_KEY は 32 バイトの鍵の base64 で指定してください") from e
    if len(seed) != SEED_BYTES:
        raise AnchorConfigError("EDGF_ANCHOR_SIGNING_KEY は 32 バイトの鍵の base64 で指定してください")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_public_key(encoded: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
    except ValueError as e:  # binascii.Error は ValueError のサブクラス
        raise AnchorConfigError("EDGF_ANCHOR_VERIFY_KEYS は 32 バイトの公開鍵の base64 で指定してください") from e


def key_id_of(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(_raw_public(key)).hexdigest()[:16]


def known_keys(s: Settings) -> dict[str, Ed25519PublicKey]:
    """検証に使える公開鍵。鍵を更新したあとも、古いアンカーは EDGF_ANCHOR_VERIFY_KEYS の公開鍵で検証する。"""
    keys = [load_public_key(k) for k in s.anchor_verify_keys]
    if s.anchor_signing_key:
        keys.append(load_private_key(s.anchor_signing_key).public_key())
    return {key_id_of(k): k for k in keys}


def validate_anchor_settings(s: Settings) -> None:
    if not s.anchor_enabled:
        return
    if s.env == "production" and not s.anchor_signing_key:
        raise AnchorConfigError("EDGF_ENV=production でアンカリングを有効にするには EDGF_ANCHOR_SIGNING_KEY が必要です (起動を中止します)")
    known_keys(s)  # 形式の誤りは環境によらず止める
    if s.anchor_tsa_url and not s.anchor_tsa_url.lower().startswith(("https://", "http://")):
        raise AnchorConfigError("EDGF_ANCHOR_TSA_URL は http(s) の URL で指定してください")


# ---------------------------------------------------------------- アンカーの形式
@dataclass(frozen=True)
class Anchor:
    seq: int
    hash: str
    anchored_at: str
    key_id: str
    signature: str = ""

    def signed_bytes(self) -> bytes:
        return json.dumps({"v": FORMAT_VERSION, "seq": self.seq, "hash": self.hash, "anchored_at": self.anchored_at, "key_id": self.key_id},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {"v": FORMAT_VERSION, "seq": self.seq, "hash": self.hash, "anchored_at": self.anchored_at, "key_id": self.key_id, "signature": self.signature}

    @classmethod
    def of_row(cls, row: AuditAnchor) -> Anchor:
        return cls(row.seq, row.hash, row.anchored_at.isoformat(), row.key_id, row.signature)


def sign(anchor: Anchor, key: Ed25519PrivateKey) -> Anchor:
    signature = base64.b64encode(key.sign(anchor.signed_bytes())).decode("ascii")
    return Anchor(anchor.seq, anchor.hash, anchor.anchored_at, anchor.key_id, signature)


def signature_ok(anchor: Anchor, keys: dict[str, Ed25519PublicKey]) -> bool:
    key = keys.get(anchor.key_id)
    if key is None:
        return False
    try:
        key.verify(base64.b64decode(anchor.signature, validate=True), anchor.signed_bytes())
    except (InvalidSignature, ValueError):
        return False
    return True


# ---------------------------------------------------------------- 書き出し先
class AnchorSink(Protocol):
    name: str

    def write(self, db: Session, anchor: Anchor) -> dict[str, Any]:
        """アンカーを書き出し、記録する内容を返す。失敗は例外で知らせる。"""
        ...


class FileAnchorSink:
    """追記だけの JSON Lines。消せないこと (WORM) は、配置先のストレージの設定で保証する。"""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, db: Session, anchor: Anchor) -> dict[str, Any]:
        line = json.dumps(anchor.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"status": "written"}


def read_anchor_file(path: Path) -> tuple[list[Anchor], list[int]]:
    """ファイルのアンカーと、読めなかった行の番号を返す。"""
    anchors, bad = [], []
    with path.open(encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, start=1):
            try:
                row = json.loads(line)
                if row["v"] != FORMAT_VERSION:
                    raise ValueError(row["v"])
                anchors.append(Anchor(int(row["seq"]), str(row["hash"]), str(row["anchored_at"]), str(row["key_id"]), str(row["signature"])))
            except (ValueError, KeyError, TypeError):
                bad.append(number)
    return anchors, bad


class OutboxAnchorSink:
    """audit.anchor イベントとして配信する。受信側が保管すれば、DB とは別の場所に控えが残る。"""

    name = "outbox"

    def write(self, db: Session, anchor: Anchor) -> dict[str, Any]:
        event = outbox.emit(db, "audit.anchor", f"audit/{anchor.seq}", anchor.to_dict())
        return {"status": "emitted", "event_id": event.id}


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 0x80:
        return bytes([tag, length]) + content
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(encoded)]) + encoded + content


SHA256_ALGORITHM = _der(0x30, _der(0x06, bytes.fromhex("608648016503040201")) + _der(0x05, b""))


def timestamp_request(digest: bytes, nonce: int) -> bytes:
    """RFC 3161 の TimeStampReq (version 1、SHA-256、nonce、certReq)。"""
    imprint = _der(0x30, SHA256_ALGORITHM + _der(0x04, digest))
    return _der(0x30, _der(0x02, b"\x01") + imprint + _der(0x02, nonce.to_bytes(8, "big").lstrip(b"\x00") or b"\x00") + _der(0x01, b"\xff"))


def _read_der(data: bytes, offset: int) -> tuple[int, int, int]:
    """タグ、内容の開始位置、内容の長さを返す。"""
    tag, first = data[offset], data[offset + 1]
    if first < 0x80:
        return tag, offset + 2, first
    size = first & 0x7F
    return tag, offset + 2 + size, int.from_bytes(data[offset + 2:offset + 2 + size], "big")


def timestamp_granted(response: bytes) -> bool:
    """TimeStampResp の PKIStatus が granted (0) か grantedWithMods (1) か。トークンの検証は保管後に openssl で行う。"""
    try:
        tag, start, _ = _read_der(response, 0)
        status_tag, status_start, _ = _read_der(response, start)
        value_tag, value_start, value_length = _read_der(response, status_start)
    except IndexError:
        return False
    return (tag, status_tag, value_tag) == (0x30, 0x30, 0x02) and int.from_bytes(response[value_start:value_start + value_length], "big") in (0, 1)


class Rfc3161Transport(Protocol):
    def post(self, url: str, body: bytes, timeout: float) -> bytes: ...


class UrllibTsaTransport:
    def post(self, url: str, body: bytes, timeout: float) -> bytes:  # pragma: no cover - 実際の TSA への通信
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/timestamp-query"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - URL は運用者の設定で、http(s) だけを起動時に確認している
            return response.read()


class Rfc3161AnchorSink:
    """TSA (時刻認証局) の署名付きトークンを取得する。アンカーがその時刻より前に存在したことの第三者の証明になる。"""

    name = "rfc3161"

    def __init__(self, url: str, transport: Rfc3161Transport | None = None, timeout: float = 10) -> None:
        self.url = url
        self.transport = transport or UrllibTsaTransport()
        self.timeout = timeout

    def write(self, db: Session, anchor: Anchor) -> dict[str, Any]:
        digest = hashlib.sha256(anchor.signed_bytes()).digest()
        try:
            response = self.transport.post(self.url, timestamp_request(digest, secrets.randbits(63)), self.timeout)
        except Exception as e:  # noqa: BLE001 - 通信の失敗の内容 (URL や応答) は記録しない
            raise AnchorSinkError(type(e).__name__) from e
        if not timestamp_granted(response):
            raise AnchorSinkError("timestamp_rejected")
        return {"status": "granted", "token": base64.b64encode(response).decode("ascii")}


def build_sinks(s: Settings, tsa_transport: Rfc3161Transport | None = None) -> list[AnchorSink]:
    """DB のトランザクションの中で使うものを先に、外部に残って取り消せないものを後ろに並べる。"""
    sinks: list[AnchorSink] = []
    if s.anchor_outbox:
        sinks.append(OutboxAnchorSink())
    if s.anchor_tsa_url:
        sinks.append(Rfc3161AnchorSink(s.anchor_tsa_url, tsa_transport))
    if s.anchor_file:
        sinks.append(FileAnchorSink(Path(s.anchor_file)))
    return sinks


# ---------------------------------------------------------------- 作成
def create_anchor(db: Session, actor: str, s: Settings | None = None, sinks: Sequence[AnchorSink] | None = None) -> dict[str, Any]:
    """チェーンの先頭にアンカーを作る。無効なら何もしない。先頭が前回と同じなら作らない。

    必須の書き出し先 (file、outbox) の失敗はジョブの失敗にして、次の実行に任せる。アンカーが無いまま成功に見せない。
    TSA は任意の第三者の証明なので、失敗しても署名付きのアンカーは残し、結果に記録する。
    """
    s = s or settings
    if not s.anchor_enabled:
        return {"anchored": 0, "reason": "disabled"}
    if not s.anchor_signing_key:
        raise AnchorConfigError("アンカリングには EDGF_ANCHOR_SIGNING_KEY が必要です")
    head = db.execute(select(AuditEvent.seq, AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)).first()
    if head is None:
        return {"anchored": 0, "reason": "empty_chain"}
    last = db.scalar(select(func.max(AuditAnchor.seq)))
    if last is not None and last >= head.seq:
        return {"anchored": 0, "reason": "no_new_events"}
    key = load_private_key(s.anchor_signing_key)
    now = utcnow()
    anchor = sign(Anchor(head.seq, head.hash, now.isoformat(), key_id_of(key.public_key())), key)
    results: dict[str, Any] = {}
    row = AuditAnchor(seq=anchor.seq, hash=anchor.hash, anchored_at=now, key_id=anchor.key_id, signature=anchor.signature, sinks={})
    db.add(row)
    db.flush()
    failed = []
    for sink in build_sinks(s) if sinks is None else sinks:
        try:
            results[sink.name] = sink.write(db, anchor)
        except AnchorSinkError as e:
            log.warning("anchor sink failed", extra={"fields": {"sink": sink.name, "error": str(e)}})
            results[sink.name] = {"status": "failed", "error": str(e)}
            failed.append(sink.name)
    row.sinks = dict(results)  # JSON 列は再代入しないと変更が検出されない
    audit.record(db, actor, "audit.anchor", "audit", str(anchor.seq), {"seq": anchor.seq, "hash": anchor.hash, "key_id": anchor.key_id,
                                                                     "sinks": {k: v["status"] for k, v in results.items()}})
    db.flush()
    return {"anchored": 1, "seq": anchor.seq, "hash": anchor.hash, "key_id": anchor.key_id, "sink_failures": failed}


# ---------------------------------------------------------------- 照合
def _public(anchor: Anchor) -> dict[str, Any]:
    return {"seq": anchor.seq, "hash": anchor.hash, "anchored_at": anchor.anchored_at, "key_id": anchor.key_id}


def _file_problem(rows: Sequence[AuditAnchor], path: Path, keys: dict[str, Ed25519PublicKey]) -> tuple[str | None, list[Anchor]]:
    """ファイルの異常の説明と、ファイルのアンカーを返す。DB に記録があるのにファイルに無い行も異常とする。"""
    expected = [r for r in rows if r.sinks.get("file", {}).get("status") == "written"]
    if not path.exists():
        return ("file_missing" if expected else None), []
    anchors, bad = read_anchor_file(path)
    if bad:
        return f"unreadable_lines:{bad[:5]}", anchors
    if keys and any(not signature_ok(a, keys) for a in anchors):
        return "signature", anchors
    written = {(a.seq, a.hash, a.signature) for a in anchors}
    if any((r.seq, r.hash, r.signature) not in written for r in expected):
        return "missing_entry", anchors
    return None, anchors


def _first_mismatch(db: Session, anchors: Sequence[Anchor]) -> int | None:
    expected = {(a.seq, a.hash) for a in anchors}
    seqs = sorted({seq for seq, _ in expected})
    actual: dict[int, str] = {}
    for start in range(0, len(seqs), CHUNK):
        actual.update({row.seq: row.hash for row in db.execute(select(AuditEvent.seq, AuditEvent.hash).where(AuditEvent.seq.in_(seqs[start:start + CHUNK])))})
    return next((seq for seq, value in sorted(expected) if actual.get(seq) != value), None)


def verify(db: Session, s: Settings | None = None) -> dict[str, Any]:
    """すべてのアンカー (DB とファイル) をチェーンと照合する。

    最新のものだけでは足りない。チェーンを計算し直されたあとに定期のアンカーが作られると、最新のアンカーは偽の先頭に署名してしまう。
    過去のアンカーは書き換え前のハッシュを持ち続けるので、全件と比べれば改ざんの後でも検知できる。

    - tail_truncated: チェーンの先頭の seq がアンカーより小さい (末尾の削除、または古いバックアップからの復元)
    - mismatch: アンカーの seq のハッシュが一致しない (チェーン全体の計算し直しを含む、アンカー以前の改ざん)
    - signature_invalid: DB のアンカーの署名が検証できない
    - file_tampered: ファイルが読めない、署名が合わない、DB の記録にある行が無い
    """
    s = s or settings
    keys = known_keys(s)
    rows = db.scalars(select(AuditAnchor).order_by(AuditAnchor.seq)).all()
    db_anchors = [Anchor.of_row(r) for r in rows]
    file_problem, file_anchors = _file_problem(rows, Path(s.anchor_file), keys) if s.anchor_file else (None, [])
    report: dict[str, Any] = {"status": "ok", "anchors_checked": len(db_anchors) + len(file_anchors), "latest": None, "head_seq": None, "detail": None}
    if keys and any(not signature_ok(a, keys) for a in db_anchors):
        return {**report, "status": "signature_invalid"}
    if file_problem:
        return {**report, "status": "file_tampered", "detail": file_problem}
    candidates = [*db_anchors, *file_anchors]
    if not candidates:
        return {**report, "status": "none"}
    latest = max(candidates, key=lambda a: a.seq)
    report["latest"] = _public(latest)
    head = db.scalar(select(func.max(AuditEvent.seq)))
    report["head_seq"] = head
    if head is None or head < latest.seq:
        return {**report, "status": "tail_truncated"}
    mismatched = _first_mismatch(db, candidates)
    if mismatched is not None:
        return {**report, "status": "mismatch", "detail": f"seq:{mismatched}"}
    return {**report, "status": "ok" if keys else "unverifiable"}


def verify_all(db: Session, s: Settings | None = None) -> dict[str, Any]:
    """チェーンの検証とアンカーの照合を合わせた結果。"""
    chain = audit.verify_chain(db)
    anchor = verify(db, s)
    return {**chain, "valid": chain["valid"] and anchor["status"] not in BROKEN, "chain_valid": chain["valid"], "anchor": anchor}


def anchoring_in_use(result: dict[str, Any], s: Settings | None = None) -> bool:
    return (s or settings).anchor_enabled or result["anchor"]["status"] != "none"


def verify_file_offline(lines: Iterable[Anchor], keys: dict[str, Ed25519PublicKey]) -> list[int]:
    """公開鍵だけでの検証 (scripts/verify_anchors.py)。署名が合わないアンカーの seq を返す。"""
    return [a.seq for a in lines if not signature_ok(a, keys)]
