#!/usr/bin/env python
"""README の権限表を edgf.authz.PERMISSIONS から再生成する (tests/test_authz.py が差分を検出する)。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EDGF_AUTH_MODE", "dev")

from edgf import authz  # noqa: E402

HEAD = "| アクション | 内容 | 許可される主体 | 承認 |\n|---|---|---|---|\n"
TAIL = "\n\nこれ以外の参照系 API"


def row(action: str, r: authz.Rule) -> str:
    who = []
    if r.authenticated:
        who.append("認証済みの全員")
    if r.roles:
        who.append("ロール: " + ", ".join(sorted(r.roles)))
    if r.domain_roles:
        who.append("ドメイン内ロール: " + ", ".join(sorted(r.domain_roles)))
    if r.relations:
        who.append("対象の " + " / ".join(sorted(r.relations)))
    return f"| `{action}` | {r.description} | {'<br>'.join(who)} | {'✓' if r.approval else ''} |"


def main() -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    start = text.index(HEAD) + len(HEAD)
    end = text.index(TAIL, start)
    table = "\n".join(row(k, v) for k, v in sorted(authz.PERMISSIONS.items()))
    path.write_text(text[:start] + table + text[end:], encoding="utf-8")


if __name__ == "__main__":
    main()
