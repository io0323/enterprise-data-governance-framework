"""§12 / §18.2 データ主体の識別子の仮名化.

Control Plane はデータ主体の識別子 (顧客 ID、メールアドレスなど) を保持しない。
API で受け取った時点で HMAC-SHA256 の subject_ref に変換し、生の値は破棄する。
Data Plane は同じ鍵 (KMS 経由) と同じ正規化で HMAC を計算し、subject_ref と突き合わせる。

    subject_ref = hex(HMAC-SHA256(key, utf8(identifier_type + "\\x1f" + normalized_value)))
    normalized_value = NFKC 正規化 + 前後の空白除去 (identifier_type が email のときは小文字化も行う)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import unicodedata

from edgf.config import Settings, settings

log = logging.getLogger("edgf.privacy")

# dev / test 専用の固定鍵。production では validate_subject_key が拒否する
DEV_SUBJECT_KEY = "edgf-dev-only-subject-key-do-not-use-in-production"
MIN_KEY_BYTES = 32


class SubjectKeyError(RuntimeError):
    """鍵の設定不備。起動を停止させる (fail fast)。"""


def validate_subject_key(s: Settings) -> None:
    if s.env == "production":
        if not s.subject_key:
            raise SubjectKeyError("EDGF_ENV=production では EDGF_SUBJECT_KEY の設定が必要です (起動を中止します)")
        if len(s.subject_key.encode("utf-8")) < MIN_KEY_BYTES:
            raise SubjectKeyError(f"EDGF_SUBJECT_KEY は {MIN_KEY_BYTES} バイト以上にしてください (起動を中止します)")
    elif not s.subject_key:
        log.warning("EDGF_SUBJECT_KEY が未設定のため固定のテスト鍵を使用します。本番環境では使用しないでください")


def normalize(identifier_type: str, value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized.lower() if identifier_type == "email" else normalized


def to_subject_ref(identifier_type: str, value: str, key: str | None = None) -> str:
    normalized = normalize(identifier_type, value)
    if not normalized:
        raise ValueError("identifier is empty")
    secret = key or settings.subject_key or DEV_SUBJECT_KEY
    return hmac.new(secret.encode("utf-8"), f"{identifier_type}\x1f{normalized}".encode(), hashlib.sha256).hexdigest()
