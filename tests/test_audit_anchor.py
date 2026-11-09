"""Phase 14d: 監査チェーンのアンカリング."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from edgf import jobs as jobs_module
from edgf.config import Settings
from edgf.db import SessionLocal, engine
from edgf.jobs import JOBS, run_job
from edgf.main import create_app
from edgf.models import AuditAnchor, AuditEvent, OutboxEvent
from edgf.services import audit, audit_anchor
from edgf.services.audit_anchor import (AnchorConfigError, Anchor, AnchorSinkError, Rfc3161AnchorSink, create_anchor, key_id_of, load_private_key, read_anchor_file,
                                        sign, signature_ok, timestamp_granted, timestamp_request, validate_anchor_settings, verify, verify_all)
from scripts.verify_anchors import main as verify_anchors_main
from tests.ai_helpers import ADMIN, API, COMMITTEE, U

SEED = bytes(range(32))
SIGNING_KEY = base64.b64encode(SEED).decode()
ROTATED = base64.b64encode(bytes(range(1, 33))).decode()


def public_b64(seed: bytes = SEED) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


@pytest.fixture()
def anchor_settings(tmp_path, monkeypatch):
    s = replace(audit_anchor.settings, anchor_enabled=True, anchor_signing_key=SIGNING_KEY, anchor_file=str(tmp_path / "anchors.jsonl"), anchor_outbox=True, anchor_tsa_url="", anchor_verify_keys=())
    monkeypatch.setattr(audit_anchor, "settings", s)
    return s


def events(client, n=3):
    for i in range(n):
        assert client.post(f"{API}/domains", headers=ADMIN, json={"name": f"d{i}"}).status_code == 201


def anchor_now():
    with SessionLocal() as db:
        result = create_anchor(db, "system")
        db.commit()
    return result


def verify_api(client, who=COMMITTEE):
    r = client.get(f"{API}/audit/verify", headers=who)
    assert r.status_code == 200
    return r.json()


def rewrite_chain(from_seq=None, new_payload=None):
    """DB を書き換えられる攻撃者: 1 行を変え、以降のハッシュをすべて計算し直してチェーンを正しく見せる。"""
    with SessionLocal() as db:
        rows = db.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all()
        target = from_seq or rows[0].seq
        prev = audit.GENESIS
        # prev_hash、hash の UNIQUE 制約に当たらないよう、いったん別の値にする
        for r in rows:
            db.execute(text("UPDATE audit_events SET prev_hash = :p, hash = :h WHERE seq = :s"), {"p": f"tmp-p-{r.seq}", "h": f"tmp-h-{r.seq}", "s": r.seq})
        db.commit()
        db.expire_all()
        for r in db.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all():
            payload = new_payload if r.seq == target and new_payload is not None else r.payload
            digest = audit._digest(prev, r.ts.isoformat(), r.actor, r.action, r.entity_type, r.entity_id, payload, r.request_id)
            r.payload, r.prev_hash, r.hash = payload, prev, digest
            prev = digest
        db.commit()
        assert audit.verify_chain(db)["valid"] is True  # チェーンの検証だけでは見つからない


# ---------------------------------------------------------------- 署名
def test_signature_round_trip_and_tamper_detection():
    key = load_private_key(SIGNING_KEY)
    keys = {key_id_of(key.public_key()): key.public_key()}
    anchor = sign(Anchor(5, "a" * 64, "2026-11-09T00:00:00", key_id_of(key.public_key())), key)
    assert signature_ok(anchor, keys)
    for changed in (replace(anchor, seq=6), replace(anchor, hash="b" * 64), replace(anchor, anchored_at="2020-01-01T00:00:00")):
        assert not signature_ok(changed, keys)
    assert not signature_ok(replace(anchor, signature="!!!"), keys)
    assert not signature_ok(replace(anchor, key_id="unknown"), keys)
    rogue = load_private_key(ROTATED)
    assert not signature_ok(sign(Anchor(5, "a" * 64, "t", anchor.key_id), rogue), keys)


# ---------------------------------------------------------------- 正常な流れ
def test_anchor_job_writes_all_sinks(client, anchor_settings):
    events(client)
    result = run_job("audit_anchor")
    assert result["status"] == "succeeded" and result["result"]["anchored"] == 1
    with SessionLocal() as db:
        row = db.scalar(select(AuditAnchor))
        head_at_anchor = db.scalar(select(AuditEvent).where(AuditEvent.seq == row.seq))
        assert row.hash == head_at_anchor.hash and row.sinks["file"]["status"] == "written" and row.sinks["outbox"]["status"] == "emitted"
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.type.like("%audit.anchor")))
        assert event.data["seq"] == row.seq and event.data["signature"] == row.signature
        # アンカー自体も監査に残る
        recorded = db.scalar(select(AuditEvent).where(AuditEvent.action == "audit.anchor"))
        assert recorded.payload["seq"] == row.seq and recorded.seq > row.seq
    lines = Path(anchor_settings.anchor_file).read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["seq"] == row.seq
    body = verify_api(client)
    assert body["valid"] is True and body["anchor"]["status"] == "ok" and body["anchor"]["latest"]["seq"] == row.seq


def test_file_contains_no_personal_data(client, anchor_settings):
    events(client)
    anchor_now()
    assert set(json.loads(Path(anchor_settings.anchor_file).read_text())) == {"v", "seq", "hash", "anchored_at", "key_id", "signature"}


def test_no_new_events_means_no_new_anchor(client, anchor_settings):
    events(client)
    with SessionLocal() as db:
        head = db.scalar(select(AuditEvent.seq).order_by(AuditEvent.seq.desc()).limit(1))
        db.add(AuditAnchor(seq=head, hash="x", anchored_at=audit.utcnow(), key_id="k", signature="s", sinks={}))
        db.commit()
        assert create_anchor(db, "system")["reason"] == "no_new_events"
    assert create_anchor(SessionLocal(), "system", replace(anchor_settings, anchor_enabled=False))["reason"] == "disabled"


def test_empty_chain_and_disabled_defaults(client, anchor_settings):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events"))
    with SessionLocal() as db:
        assert create_anchor(db, "system")["reason"] == "empty_chain"
        assert verify(db)["status"] == "none"
    assert JOBS["audit_anchor"].default_interval == 3600


def test_job_is_a_no_op_when_disabled(client):
    events(client)
    assert run_job("audit_anchor")["result"]["reason"] == "disabled"
    assert verify_api(client).keys() == {"valid", "events_checked", "broken_at_seq"}  # 使っていなければ応答は従来と同じ


def test_enabled_without_anchors_reports_none(client, anchor_settings):
    events(client)
    body = verify_api(client)
    assert body["valid"] is True and body["anchor"]["status"] == "none"


def test_overdue_warning_only_when_anchoring_is_enabled(client, monkeypatch):
    job = JOBS["audit_anchor"]
    old, now = audit.utcnow() - timedelta(days=30), audit.utcnow()
    assert jobs_module._is_overdue(job, None, old, now) is False
    monkeypatch.setattr(jobs_module, "settings", replace(jobs_module.settings, anchor_enabled=True))
    assert jobs_module._is_overdue(job, None, old, now) is True


def test_missing_key_fails_the_job_outside_production(client, anchor_settings, monkeypatch):
    monkeypatch.setattr(audit_anchor, "settings", replace(anchor_settings, anchor_signing_key=""))
    events(client)
    result = run_job("audit_anchor")
    assert result["status"] == "failed" and result["error"] == "AnchorConfigError"


# ---------------------------------------------------------------- 改ざんの検知
def test_tail_deletion_is_detected(client, anchor_settings):
    events(client)
    anchor_now()
    with engine.begin() as conn:
        head = conn.execute(text("SELECT max(seq) FROM audit_anchors")).scalar()
        conn.execute(text("DELETE FROM audit_events WHERE seq >= :s"), {"s": head})
    body = verify_api(client)
    assert body["chain_valid"] is True  # 末尾を消してもチェーンは正しいまま
    assert body["valid"] is False and body["anchor"]["status"] == "tail_truncated" and body["anchor"]["head_seq"] < body["anchor"]["latest"]["seq"]


def test_tail_deletion_with_db_anchor_removed_is_detected_by_file(client, anchor_settings):
    events(client)
    anchor_now()
    with engine.begin() as conn:
        seq = conn.execute(text("SELECT max(seq) FROM audit_anchors")).scalar()
        conn.execute(text("DELETE FROM audit_anchors"))
        conn.execute(text("DELETE FROM audit_events WHERE seq >= :s"), {"s": seq})
    body = verify_api(client)
    assert body["valid"] is False and body["anchor"]["status"] == "tail_truncated"


def test_single_row_tamper_before_anchor_breaks_chain(client, anchor_settings):
    events(client)
    anchor_now()
    with engine.begin() as conn:
        first = conn.execute(text("SELECT min(seq) FROM audit_events")).scalar()
        conn.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE seq = :s"), {"s": first})
    body = verify_api(client)
    assert body["valid"] is False and body["chain_valid"] is False and body["broken_at_seq"] == first


def test_recomputed_chain_is_detected_by_anchor(client, anchor_settings):
    """全行を計算し直すとチェーンの検証は通る。アンカーとの照合で初めて見つかる。"""
    events(client)
    anchor_now()
    rewrite_chain(new_payload={"forged": True})
    body = verify_api(client)
    assert body["chain_valid"] is True and body["valid"] is False and body["anchor"]["status"] == "mismatch"


def test_rewrite_stays_detected_after_the_next_anchor(client, anchor_settings):
    """書き換えのあとに定期のアンカーが作られても (偽の先頭に署名されても)、過去のアンカーとの照合で見つかる。"""
    events(client)
    anchor_now()
    rewrite_chain(new_payload={"forged": True})
    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": "later"}).status_code == 201
    assert anchor_now()["anchored"] == 1
    body = verify_api(client)
    assert body["chain_valid"] is True and body["valid"] is False and body["anchor"]["status"] == "mismatch"


def test_file_anchor_detects_rewrite_when_db_anchors_are_replaced(client, anchor_settings):
    """DB のアンカーを全部消して作り直しても、ファイルの過去のアンカーと一致しない。"""
    events(client)
    anchor_now()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_anchors"))
    rewrite_chain(new_payload={"forged": True})
    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": "later"}).status_code == 201
    assert anchor_now()["anchored"] == 1
    assert verify_api(client)["anchor"]["status"] == "mismatch"


def test_many_anchors_are_all_compared(client, anchor_settings, monkeypatch):
    monkeypatch.setattr(audit_anchor, "CHUNK", 2)
    events(client, 1)
    for i in range(5):
        assert client.post(f"{API}/domains", headers=ADMIN, json={"name": f"m{i}"}).status_code == 201
        assert anchor_now()["anchored"] == 1
    assert verify_api(client)["anchor"]["status"] == "ok"
    with engine.begin() as conn:
        seq = conn.execute(text("SELECT min(seq) FROM audit_anchors")).scalar()
        conn.execute(text("UPDATE audit_anchors SET hash = :h WHERE seq > :s"), {"h": "f" * 64, "s": seq + 10**6})
    with SessionLocal() as db:
        anchors = [Anchor.of_row(r) for r in db.scalars(select(AuditAnchor)).all()]
        assert audit_anchor._first_mismatch(db, anchors) is None
        assert audit_anchor._first_mismatch(db, [replace(anchors[0], hash="0" * 64), *anchors[1:]]) == anchors[0].seq


def test_recomputed_chain_after_anchor_is_not_flagged_as_mismatch(client, anchor_settings):
    """アンカー以降の行の書き換えは、次のアンカーまでチェーンの検証の範囲 (既知の限界)。"""
    events(client)
    anchor_now()
    with SessionLocal() as db:
        head = db.scalar(select(AuditAnchor.seq))
    with engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE seq > :s"), {"s": head})
    body = verify_api(client)
    assert body["valid"] is False and body["chain_valid"] is False and body["anchor"]["status"] == "ok"


@pytest.mark.parametrize("attack", ["edit_hash", "edit_seq", "garbage", "delete_line", "delete_file", "forged_with_other_key"])
def test_anchor_file_tampering_is_detected(client, anchor_settings, attack):
    events(client)
    anchor_now()
    path = Path(anchor_settings.anchor_file)
    row = json.loads(path.read_text())
    if attack == "edit_hash":
        path.write_text(json.dumps({**row, "hash": "0" * 64}) + "\n")
    elif attack == "edit_seq":
        path.write_text(json.dumps({**row, "seq": row["seq"] + 1000}) + "\n")
    elif attack == "garbage":
        path.write_text(path.read_text() + "not json\n")
    elif attack == "delete_line":
        path.write_text("")
    elif attack == "delete_file":
        path.unlink()
    else:
        rogue = load_private_key(ROTATED)
        forged = sign(Anchor(row["seq"] + 1000, "1" * 64, row["anchored_at"], key_id_of(rogue.public_key())), rogue)
        path.write_text(path.read_text() + json.dumps(forged.to_dict()) + "\n")
    body = verify_api(client)
    assert body["valid"] is False and body["anchor"]["status"] == "file_tampered", body


def test_db_anchor_tampering_is_detected(client, anchor_settings):
    events(client)
    anchor_now()
    with engine.begin() as conn:
        conn.execute(text("UPDATE audit_anchors SET hash = :h"), {"h": "0" * 64})
    body = verify_api(client)
    assert body["valid"] is False and body["anchor"]["status"] == "signature_invalid"


def test_old_backup_restore_is_reported_as_truncated(client, anchor_settings):
    """古いバックアップから戻すと、バックアップより新しいアンカーがファイルに残るので欠落が分かる。"""
    events(client, 1)
    anchor_now()
    with SessionLocal() as db:
        first_head = db.scalar(select(AuditEvent.seq).order_by(AuditEvent.seq.desc()).limit(1))
    events_after = client.post(f"{API}/domains", headers=ADMIN, json={"name": "later"})
    assert events_after.status_code == 201
    anchor_now()
    with engine.begin() as conn:  # バックアップの時点まで戻す
        conn.execute(text("DELETE FROM audit_anchors WHERE seq > :s"), {"s": first_head})
        conn.execute(text("DELETE FROM audit_events WHERE seq > :s"), {"s": first_head})
    body = verify_api(client)
    assert body["anchor"]["status"] == "tail_truncated" and body["valid"] is False


# ---------------------------------------------------------------- 鍵
def test_key_rotation_keeps_old_anchors_verifiable(client, anchor_settings, monkeypatch):
    events(client)
    anchor_now()
    rotated = replace(anchor_settings, anchor_signing_key=ROTATED, anchor_verify_keys=(public_b64(),))
    monkeypatch.setattr(audit_anchor, "settings", rotated)
    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": "later"}).status_code == 201
    anchor_now()
    assert verify_api(client)["anchor"]["status"] == "ok"
    monkeypatch.setattr(audit_anchor, "settings", replace(rotated, anchor_verify_keys=()))
    assert verify_api(client)["anchor"]["status"] == "signature_invalid"  # 古い公開鍵を消すと検証できない


def test_verification_without_any_key_is_unverifiable(client, anchor_settings, monkeypatch):
    events(client)
    anchor_now()
    monkeypatch.setattr(audit_anchor, "settings", replace(anchor_settings, anchor_signing_key="", anchor_enabled=False))
    body = verify_api(client)
    assert body["anchor"]["status"] == "unverifiable" and body["valid"] is True


@pytest.mark.parametrize("bad", ["", "x", "not base64!!", base64.b64encode(b"short").decode()])
def test_invalid_private_key_refuses_to_start(bad):
    s = Settings(auth_mode="dev", env="test", anchor_enabled=True, anchor_signing_key=bad)
    if bad == "":
        validate_anchor_settings(s)  # production 以外では鍵なしでも起動する (ジョブが失敗する)
    else:
        with pytest.raises(AnchorConfigError):
            validate_anchor_settings(s)


def test_invalid_public_key_refuses_to_start():
    with pytest.raises(AnchorConfigError):
        validate_anchor_settings(Settings(auth_mode="dev", env="test", anchor_enabled=True, anchor_verify_keys=("###",)))
    with pytest.raises(AnchorConfigError):
        validate_anchor_settings(Settings(auth_mode="dev", env="test", anchor_enabled=True, anchor_tsa_url="ftp://x"))


def _production(**kw) -> Settings:
    return Settings(auth_mode="jwt", env="production", oidc_issuer="https://i", oidc_audience="a", oidc_jwks_url="https://i/j",
                    subject_key="k" * 32, **kw)


def test_production_refuses_to_start_without_key():
    with pytest.raises(AnchorConfigError, match="EDGF_ANCHOR_SIGNING_KEY"):
        create_app(_production(anchor_enabled=True))


def test_production_starts_with_key_or_when_disabled():
    create_app(_production(anchor_enabled=True, anchor_signing_key=SIGNING_KEY))
    create_app(_production(anchor_enabled=False))


# ---------------------------------------------------------------- 書き出し先の失敗
class FailingSink:
    name = "file"

    def write(self, db, anchor):
        raise OSError("disk full")


def test_required_sink_failure_fails_the_job_and_leaves_no_anchor(client, anchor_settings, monkeypatch):
    events(client)
    monkeypatch.setattr(audit_anchor, "build_sinks", lambda s: [FailingSink()])
    result = run_job("audit_anchor")
    assert result["status"] == "failed" and result["error"] == "OSError"
    with SessionLocal() as db:
        assert db.scalar(select(AuditAnchor)) is None


class FakeTsa:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def post(self, url, body, timeout):
        self.requests.append((url, body))
        if self.error:
            raise self.error
        return self.response




def _response(status: int) -> bytes:
    pki = audit_anchor._der(0x30, audit_anchor._der(0x02, bytes([status])))
    return audit_anchor._der(0x30, pki + audit_anchor._der(0x30, b"\x01" * 200))


def test_rfc3161_request_shape():
    digest = hashlib.sha256(b"x").digest()
    request = timestamp_request(digest, 12345)
    assert request[0] == 0x30 and digest in request and bytes.fromhex("608648016503040201") in request and request.endswith(bytes.fromhex("0101ff"))
    assert timestamp_request(digest, 1) != timestamp_request(digest, 2)


@pytest.mark.parametrize("status, granted", [(0, True), (1, True), (2, False), (5, False)])
def test_rfc3161_status(status, granted):
    assert timestamp_granted(_response(status)) is granted


@pytest.mark.parametrize("raw", [b"", b"\x30", b"\x05\x00", b"<html>error</html>"])
def test_rfc3161_garbage(raw):
    assert timestamp_granted(raw) is False


def test_tsa_token_is_kept_and_failure_does_not_lose_the_anchor(client, anchor_settings, monkeypatch):
    events(client)
    fake = FakeTsa(_response(0))
    with SessionLocal() as db:
        result = create_anchor(db, "system", sinks=[Rfc3161AnchorSink("https://tsa.test/", fake)])
        db.commit()
        row = db.scalar(select(AuditAnchor))
        assert result["sink_failures"] == [] and row.sinks["rfc3161"]["status"] == "granted"
        assert base64.b64decode(row.sinks["rfc3161"]["token"]) == fake.response and fake.requests[0][0] == "https://tsa.test/"
    listed = client.get(f"{API}/audit/anchors", headers=COMMITTEE).json()
    assert listed[0]["sinks"]["rfc3161"] == {"status": "granted"}  # トークンの本体は一覧に出さない

    assert client.post(f"{API}/domains", headers=ADMIN, json={"name": "later"}).status_code == 201
    for fake, reason in ((FakeTsa(error=ConnectionError("https://user:secret@tsa")), "ConnectionError"), (FakeTsa(_response(2)), "timestamp_rejected")):
        assert client.post(f"{API}/domains", headers=ADMIN, json={"name": f"x{reason}"}).status_code == 201
        with SessionLocal() as db:
            result = create_anchor(db, "system", sinks=[Rfc3161AnchorSink("https://tsa.test/", fake)])
            db.commit()
            row = db.scalar(select(AuditAnchor).order_by(AuditAnchor.seq.desc()).limit(1))
        assert result["sink_failures"] == ["rfc3161"] and row.sinks["rfc3161"] == {"status": "failed", "error": reason}
    with pytest.raises(AnchorSinkError):
        Rfc3161AnchorSink("https://tsa.test/", FakeTsa(error=OSError())).write(None, Anchor(1, "a", "t", "k"))


def test_build_sinks_follows_settings(tmp_path):
    base = Settings(auth_mode="dev", env="test")
    assert [s.name for s in audit_anchor.build_sinks(replace(base, anchor_outbox=True))] == ["outbox"]
    assert audit_anchor.build_sinks(replace(base, anchor_outbox=False)) == []
    names = [s.name for s in audit_anchor.build_sinks(replace(base, anchor_outbox=True, anchor_file=str(tmp_path / "a"), anchor_tsa_url="https://t"))]
    assert names == ["outbox", "rfc3161", "file"]


# ---------------------------------------------------------------- API
def test_anchor_details_require_audit_read(client, anchor_settings):
    events(client)
    anchor_now()
    consumer = U("analyst")
    plain = verify_api(client, consumer)
    assert set(plain) == {"valid", "events_checked", "broken_at_seq"} and plain["valid"] is True
    assert "anchor" in verify_api(client, ADMIN) and "anchor" in verify_api(client, COMMITTEE)
    for who, status in ((consumer, 403), (U("o", "owner,steward,custodian", "*"), 403), (ADMIN, 200), (COMMITTEE, 200)):
        assert client.get(f"{API}/audit/anchors", headers=who).status_code == status


def test_consumer_sees_valid_false_when_anchor_breaks(client, anchor_settings):
    events(client)
    anchor_now()
    rewrite_chain(new_payload={"forged": True})
    assert verify_api(client, U("analyst"))["valid"] is False


def test_manual_run_is_admin_only(client, anchor_settings):
    events(client)
    assert client.post(f"{API}/ops/jobs/audit_anchor/run", headers=COMMITTEE).status_code == 403
    assert client.post(f"{API}/ops/jobs/audit_anchor/run", headers=ADMIN).status_code in (200, 202)
    assert len(client.get(f"{API}/audit/anchors", headers=COMMITTEE).json()) == 1


# ---------------------------------------------------------------- 外部の検証
def test_offline_verification_script(client, anchor_settings, tmp_path, capsys):
    events(client)
    anchor_now()
    path = anchor_settings.anchor_file
    assert verify_anchors_main([path, "--public-key", public_b64()]) == 0
    assert json.loads(capsys.readouterr().out)["anchors"] == 1
    assert verify_anchors_main([path, "--public-key", public_b64(bytes(range(1, 33)))]) == 1
    capsys.readouterr()
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert verify_anchors_main([str(empty), "--public-key", public_b64()]) == 1
    capsys.readouterr()
    tampered = tmp_path / "bad.jsonl"
    row = json.loads(Path(path).read_text())
    tampered.write_text(json.dumps({**row, "hash": "0" * 64}) + "\nnot json\n")
    assert verify_anchors_main([str(tampered), "--public-key", public_b64()]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["bad_lines"] == [2] and report["bad_signature_seqs"] == [row["seq"]]


def test_unsupported_format_is_unreadable(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps({"v": 99, "seq": 1, "hash": "a", "anchored_at": "t", "key_id": "k", "signature": "s"}) + "\n" + json.dumps({"v": 1}) + "\n")
    assert read_anchor_file(path) == ([], [1, 2])
