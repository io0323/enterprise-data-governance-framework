"""一覧系 API のページネーション統一 (limit / offset).

後方互換のためレスポンスボディは従来どおり JSON 配列のまま返し、ページ情報はヘッダで返す:
- X-Total-Count: フィルタ適用後の総件数
- Link (RFC 8288): rel="next" / rel="prev" の相対 URL
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import Query, Request, Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

MAX_LIMIT = 1000
DEFAULT_LIMIT = 100
T = TypeVar("T")


@dataclass(frozen=True)
class Page:
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def page_params(default_limit: int = DEFAULT_LIMIT) -> Callable[..., Page]:
    def dep(limit: int = Query(default_limit, ge=1, le=MAX_LIMIT, description=f"取得件数 (1〜{MAX_LIMIT})"),
            offset: int = Query(0, ge=0, description="先頭からのスキップ件数")) -> Page:
        return Page(limit=limit, offset=offset)
    return dep


def set_page_headers(request: Request, response: Response, page: Page, total: int) -> None:
    response.headers["X-Total-Count"] = str(total)
    links = []
    if page.offset + page.limit < total:
        links.append(("next", page.offset + page.limit))
    if page.offset > 0:
        links.append(("prev", max(page.offset - page.limit, 0)))
    if links:
        parts = []
        for rel, off in links:
            u = request.url.include_query_params(limit=page.limit, offset=off)
            parts.append(f'<{u.path}?{u.query}>; rel="{rel}"')
        response.headers["Link"] = ", ".join(parts)


def paginate(db: Session, stmt: Select[Any], page: Page, request: Request, response: Response) -> Sequence[Any]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = db.scalars(stmt.limit(page.limit).offset(page.offset)).all()
    set_page_headers(request, response, page, total)
    return items


def paginate_list(items: Sequence[T], page: Page, request: Request, response: Response) -> list[T]:
    set_page_headers(request, response, page, len(items))
    return list(items[page.offset:page.offset + page.limit])
