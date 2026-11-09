#!/usr/bin/env python
"""アンカーファイルを公開鍵だけで検証する (DB にも署名鍵にも触らない。外部の監査人が使う想定).

    python scripts/verify_anchors.py anchors.jsonl --public-key <base64> [--public-key <古い鍵>]

署名が合わない行、読めない行があると終了コード 1 を返す。最後の行の seq と hash を /audit/verify の結果と比べる。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgf.services.audit_anchor import key_id_of, load_public_key, read_anchor_file, verify_file_offline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--public-key", action="append", required=True)
    args = parser.parse_args(argv)
    keys = {key_id_of(k): k for k in map(load_public_key, args.public_key)}
    anchors, bad_lines = read_anchor_file(args.file)
    bad_seqs = verify_file_offline(anchors, keys)
    latest = max(anchors, key=lambda a: a.seq, default=None)
    print(json.dumps({"anchors": len(anchors), "bad_lines": bad_lines, "bad_signature_seqs": bad_seqs,
                      "latest": None if latest is None else {"seq": latest.seq, "hash": latest.hash, "anchored_at": latest.anchored_at}}, ensure_ascii=False))
    return 1 if bad_lines or bad_seqs or not anchors else 0


if __name__ == "__main__":
    sys.exit(main())
