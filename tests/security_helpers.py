"""Phase 14c のセキュリティテストの共通部品: ルートの列挙と、ルートから到達できる権限の静的な解析."""
from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from types import FunctionType, ModuleType
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from edgf import authz

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class RouteInfo:
    path: str
    methods: frozenset[str]
    route: APIRoute

    @property
    def mutating(self) -> bool:
        return bool(self.methods - SAFE_METHODS)

    @property
    def params(self) -> list[str]:
        return re.findall(r"{(\w+)(?::\w+)?}", self.path)


def _walk(routes: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for r in routes:
        included = getattr(r, "original_router", None)  # 新しい FastAPI の include_router は遅延して展開される
        if included is not None:
            yield from _walk(included.routes, prefix + r.include_context.prefix)
        else:
            yield prefix, r


def api_routes(app: FastAPI) -> list[RouteInfo]:
    return [RouteInfo(prefix + r.path, frozenset(r.methods), r) for prefix, r in _walk(app.routes) if isinstance(r, APIRoute)]


def _module_of(fn: FunctionType) -> ModuleType:
    return sys.modules[fn.__module__]


def _resolve(node: ast.expr, namespace: dict[str, Any]) -> Any:
    """`f` と `module.f` を、関数の定義元の名前空間で解決する。"""
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, namespace)
        return getattr(base, node.attr, None) if isinstance(base, ModuleType) else None
    return None


def _edgf_function(obj: Any) -> FunctionType | None:
    obj = inspect.unwrap(obj) if callable(obj) else obj
    return obj if isinstance(obj, FunctionType) and obj.__module__.startswith("edgf.") else None


@lru_cache(maxsize=None)
def _direct(fn: FunctionType) -> tuple[frozenset[str], tuple[FunctionType, ...]]:
    """関数が直接参照する権限の文字列と、呼び出す edgf の関数。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    namespace = vars(_module_of(fn))
    actions, callees = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in authz.PERMISSIONS:
            actions.add(node.value)
        elif isinstance(node, ast.Name | ast.Attribute):
            callee = _edgf_function(_resolve(node, namespace))
            if callee is not None:
                callees.append(callee)
    return frozenset(actions), tuple(callees)


def reachable_actions(fn: FunctionType) -> frozenset[str]:
    """関数から呼び出しを辿って到達できる権限。依存関係の解析ではなく、保護の有無の検査に使う。"""
    seen: set[FunctionType] = set()
    stack = [inspect.unwrap(fn)]
    found: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        actions, callees = _direct(current)
        found |= actions
        stack.extend(callees)
    return frozenset(found)
