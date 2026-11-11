"""Problem Details (RFC 9457) を型付きの例外にする."""
from __future__ import annotations

from typing import Any

import httpx

PROBLEM_PREFIX = "urn:edgf:problem:"


class EdgfError(Exception):
    """SDK の例外の基底。"""


class TransportError(EdgfError):
    """再試行しても Control Plane に届かなかった。"""


class TokenError(EdgfError):
    """トークンエンドポイントからトークンを取得できない。"""


class ProblemError(EdgfError):
    """Control Plane が返した Problem Details。type で分岐する (slug は urn:edgf:problem: を除いた部分)。"""

    status: int = 0

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        self.status = status
        self.problem = problem
        self.type: str = problem.get("type") or ""
        self.slug: str = self.type.removeprefix(PROBLEM_PREFIX)
        self.title: str = problem.get("title") or ""
        self.detail: str | None = problem.get("detail")
        self.instance: str | None = problem.get("instance")
        self.request_id: str | None = problem.get("request_id")
        self.extensions = {k: v for k, v in problem.items() if k not in ("type", "title", "status", "detail", "instance", "request_id")}
        super().__init__(f"{status} {self.slug or self.title}: {self.detail or ''} (request_id={self.request_id})")


class BadRequest(ProblemError):
    pass


class Unauthenticated(ProblemError):
    pass


class Forbidden(ProblemError):
    """403。action と required (必要な権限) が extensions に入る。"""


class NotFound(ProblemError):
    pass


class Conflict(ProblemError):
    pass


class PayloadTooLarge(ProblemError):
    pass


class ValidationFailed(ProblemError):
    """422。errors (項目ごとの理由) が extensions に入ることがある。"""

    @property
    def errors(self) -> list[dict[str, Any]]:
        return list(self.extensions.get("errors") or [])


class RateLimited(ProblemError):
    pass


class ServerError(ProblemError):
    pass


class ServiceUnavailable(ServerError):
    pass


_BY_STATUS: dict[int, type[ProblemError]] = {400: BadRequest, 401: Unauthenticated, 403: Forbidden, 404: NotFound, 409: Conflict,
                                              413: PayloadTooLarge, 422: ValidationFailed, 429: RateLimited, 503: ServiceUnavailable}


def problem_from_response(response: httpx.Response) -> ProblemError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):  # Problem Details でない応答 (プロキシのエラーなど)
        body = {"title": response.reason_phrase, "detail": response.text[:500]}
    status = response.status_code
    cls = _BY_STATUS.get(status) or (ServerError if status >= 500 else ProblemError)
    return cls(status, body)
