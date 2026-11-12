"""§9 カラム単位のリネージの適用: 信頼できる生成元と、PII の伝播 (Phase 16)。

広げる方向 (PII のカラムの追加) は、どの辺からでも行う。
絞る方向 (消去の対象・AI の版・PII の継承から外す) は、データセットの辺の組 (A → B) が「信頼できる」ときだけ行う。

組が信頼できるのは、次をすべて満たすとき:
- A → B のデータセット単位の辺の job が、すべて Policy Bundle の lineage/trusted_producers.yaml で宣言されていること
- その job ごとに A → B のカラムの辺があり、すべて OpenLineage で、宣言された主体から取り込まれていること
  (手動の登録や、別の主体が job の名前を名乗った辺では絞らない)

信頼できない組は、従来どおりデータセット単位で伝播する (A のどこかに PII があれば B 全体)。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf.models import ColumnLineageEdge, LineageEdge
from edgf.services import lineage
from edgf.services.column_lineage import flows_values

TRUST_FILE = "lineage/trusted_producers.yaml"
VIA_OPENLINEAGE = "openlineage"
VIA_MANUAL = "manual"
REASON = "no_direct_path_from_pii_columns"
CHUNK = 500
_ENTRY_KEYS = {"job_namespace", "jobs", "principals", "reason"}


class TrustFileInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Producer:
    job_namespace: str
    jobs: tuple[str, ...]  # 空なら名前空間のすべての job
    principals: frozenset[str]

    def matches(self, job: str, recorded_by: str | None) -> bool:
        if recorded_by not in self.principals or not job.startswith(self.job_namespace + "/"):
            return False
        return not self.jobs or job.removeprefix(self.job_namespace + "/") in self.jobs


def parse(text: str) -> tuple[Producer, ...]:
    """trusted_producers.yaml を検証して読み込む。主体の無い宣言は、名乗るだけで信頼されてしまうので拒否する。"""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise TrustFileInvalid(f"{TRUST_FILE}: YAML を解釈できません") from e
    if not isinstance(doc, dict) or set(doc) - {"producers"}:
        raise TrustFileInvalid(f"{TRUST_FILE}: producers だけを持つオブジェクトで指定してください")
    entries = doc.get("producers")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise TrustFileInvalid(f"{TRUST_FILE}: producers は配列で指定してください")
    out = []
    for i, entry in enumerate(entries):
        where = f"{TRUST_FILE}: producers[{i}]"
        if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
            raise TrustFileInvalid(f"{where}: 使えるキーは {sorted(_ENTRY_KEYS)} です")
        namespace, jobs, principals = entry.get("job_namespace"), entry.get("jobs", []), entry.get("principals")
        if not isinstance(namespace, str) or not namespace or namespace.endswith("/"):
            raise TrustFileInvalid(f"{where}: job_namespace (文字列) が必要です")
        if not isinstance(jobs, list) or not all(isinstance(j, str) and j for j in jobs):
            raise TrustFileInvalid(f"{where}: jobs は job 名の配列で指定してください (省略で名前空間のすべて)")
        if not isinstance(principals, list) or not principals or not all(isinstance(p, str) and p for p in principals):
            raise TrustFileInvalid(f"{where}: principals (イベントを送る主体の配列) が必要です")
        out.append(Producer(namespace, tuple(jobs), frozenset(principals)))
    return tuple(out)


def producers(db: Session) -> tuple[Producer, ...]:
    """active の Policy Bundle の宣言。承認されて公開されるまで、ディスクの変更は反映しない。"""
    from edgf.services import policy_bundles  # 循環 import 回避

    bundle = policy_bundles.active_bundle(db)
    text = (bundle.files or {}).get(TRUST_FILE) if bundle else None
    return parse(text) if text else ()


def edge_trusted(edge: ColumnLineageEdge, declared: Iterable[Producer]) -> bool:
    return edge.via == VIA_OPENLINEAGE and any(p.matches(edge.job, edge.recorded_by) for p in declared)


@dataclass
class Pair:
    trusted: bool
    jobs: list[str]
    flows: list[tuple[str, str]]  # 値の流れるカラムの辺 (source_column, target_column)


def _chunks(names: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(names), CHUNK):
        yield names[start:start + CHUNK]


def pairs(db: Session, wanted: Iterable[tuple[str, str]], declared: tuple[Producer, ...]) -> dict[tuple[str, str], Pair]:
    """データセットの辺の組ごとに、信頼できるかと、値の流れるカラムの辺を返す。"""
    wanted = set(wanted)
    sources = sorted({a for a, _ in wanted})
    jobs: dict[tuple[str, str], set[str]] = {}
    columns: dict[tuple[str, str], list[ColumnLineageEdge]] = {}
    for chunk in _chunks(sources):
        for e in db.scalars(select(LineageEdge).where(LineageEdge.source.in_(chunk))):
            if (e.source, e.target) in wanted:
                jobs.setdefault((e.source, e.target), set()).add(e.job)
        for c in db.scalars(select(ColumnLineageEdge).where(ColumnLineageEdge.source.in_(chunk)).order_by(ColumnLineageEdge.id)):
            if (c.source, c.target) in wanted:
                columns.setdefault((c.source, c.target), []).append(c)
    out = {}
    for key in wanted:
        edges = columns.get(key, [])
        by_job: dict[str, list[ColumnLineageEdge]] = {}
        for c in edges:
            by_job.setdefault(c.job, []).append(c)
        names = jobs.get(key, set())
        trusted = bool(declared) and bool(names) and all(
            by_job.get(j) and all(edge_trusted(c, declared) for c in by_job[j]) for j in names)
        flows = [(c.source_column, c.target_column) for c in edges if c.target_column is not None and flows_values(c.type, c.subtype)]
        out[key] = Pair(trusted=trusted, jobs=sorted(names), flows=flows)
    return out


@dataclass
class Taint:
    """PII の伝播の結果。whole はデータセット全体 (カラムが分からない)、columns は値の流れが分かったカラム。"""
    whole: set[str] = field(default_factory=set)
    columns: dict[str, set[str]] = field(default_factory=dict)
    depth: dict[str, int] = field(default_factory=dict)  # データセット単位の最短の深さ
    excluded: list[dict[str, Any]] = field(default_factory=list)

    def tainted(self, name: str) -> bool:
        return name in self.whole or bool(self.columns.get(name))


def forward(db: Session, roots: Mapping[str, Iterable[str] | None], max_depth: int) -> Taint:
    """起点の PII のカラム (None はデータセット全体) から下流へ伝播する。

    下流に届いても PII が流れていない資産は excluded に、理由とともに入る (信頼できる組だけを経由した場合に限られる)。
    """
    taint = Taint()
    for name, cols in roots.items():
        if cols is None:
            taint.whole.add(name)
        else:
            taint.columns[name] = set(cols)
    wanted: set[tuple[str, str]] = set()
    for root in sorted(roots):
        for edge in lineage.traverse(db, root, "downstream", max_depth):
            wanted.add((edge["from"], edge["to"]))
            if edge["to"] not in roots:
                taint.depth[edge["to"]] = min(taint.depth.get(edge["to"], edge["depth"]), edge["depth"])
    graph = pairs(db, wanted, producers(db))
    outgoing: dict[str, list[str]] = {}
    for a, b in sorted(graph):
        outgoing.setdefault(a, []).append(b)
    queue = deque(sorted(roots))
    while queue:
        a = queue.popleft()
        whole, known = a in taint.whole, taint.columns.get(a, set())
        if not whole and not known:
            continue
        for b in outgoing.get(a, []):
            pair = graph[(a, b)]
            changed = False
            if not pair.trusted and b not in taint.whole:
                taint.whole.add(b)
                changed = True
            new = {tc for sc, tc in pair.flows if sc in known or (whole and pair.trusted)}
            if new - taint.columns.get(b, set()):
                taint.columns.setdefault(b, set()).update(new)
                changed = True
            if changed:
                queue.append(b)
    incoming: dict[str, list[tuple[str, Pair]]] = {}
    for (a, b), pair in sorted(graph.items()):
        incoming.setdefault(b, []).append((a, pair))
    for name in sorted(taint.depth):
        if not taint.tainted(name):
            incoming_pairs = incoming.get(name, [])
            taint.excluded.append({"qualified_name": name, "reason": REASON, "via": [a for a, _ in incoming_pairs],
                                   "trusted_producers": sorted({j for _, p in incoming_pairs for j in p.jobs})})
    return taint


@dataclass
class Need:
    """後ろ向きの伝播: 起点の資産に値が届きうる上流のカラム。"""
    whole: set[str] = field(default_factory=set)
    columns: dict[str, set[str]] = field(default_factory=dict)
    graph: dict[tuple[str, str], Pair] = field(default_factory=dict)

    def reaches(self, name: str, pii_columns: Iterable[str]) -> bool:
        """name の PII が起点に届きうるか。PII のカラムが宣言されていなければ、どこかが届けば届くとみなす。"""
        if name in self.whole:
            return True
        cols, pii = self.columns.get(name, set()), set(pii_columns)
        return bool(cols & pii) if pii else bool(cols)


def backward(db: Session, target: str, edges: Iterable[tuple[str, str]]) -> Need:
    """target から、与えたデータセットの辺 (上流の探索で集めたもの) を遡る。信頼できる組だけカラムで絞る。"""
    graph = pairs(db, edges, producers(db))
    incoming: dict[str, list[str]] = {}
    for a, b in sorted(graph):
        incoming.setdefault(b, []).append(a)
    need = Need(whole={target}, graph=graph)
    queue = deque([target])
    while queue:
        b = queue.popleft()
        whole, known = b in need.whole, need.columns.get(b, set())
        for a in incoming.get(b, []):
            pair = graph[(a, b)]
            changed = False
            if not pair.trusted:
                if a not in need.whole:
                    need.whole.add(a)
                    changed = True
            else:
                new = {sc for sc, tc in pair.flows if whole or tc in known}
                if new - need.columns.get(a, set()):
                    need.columns.setdefault(a, set()).update(new)
                    changed = True
            if changed:
                queue.append(a)
    return need


def column_flow(graph: Mapping[tuple[str, str], Pair], starts: Mapping[str, Iterable[str]], target: str) -> dict[str, list[dict[str, str]]]:
    """広げる方向: 値の流れるカラムの辺を、信頼の有無によらず辿って、target のどのカラムに PII のカラムが届くか。"""
    reached: dict[tuple[str, str], set[tuple[str, str]]] = {}  # (資産, カラム) → 届いた元 (資産, カラム)
    origin = {(name, col) for name, cols in starts.items() for col in cols}
    queue = deque(sorted(origin))
    for node in origin:
        reached[node] = {node}
    outgoing: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for (a, b), pair in graph.items():
        for sc, tc in pair.flows:
            outgoing.setdefault((a, sc), []).append((b, tc))
    while queue:
        node = queue.popleft()
        for nxt in outgoing.get(node, []):
            before = reached.get(nxt, set())
            after = before | reached[node]
            if after != before:
                reached[nxt] = after
                queue.append(nxt)
    return {col: [{"asset": a, "column": c} for a, c in sorted(origins)]
            for (name, col), origins in sorted(reached.items()) if name == target and (name, col) not in origin}
