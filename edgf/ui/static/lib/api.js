// REST API の読み取り。GET だけを送る。認証はセッションの Cookie (HttpOnly) で、トークンは扱わない。
// X-Actor は送らない (UI は dev モードに対応しない)。書き込みは actions.js から送る。
import * as session from "./session.js";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, problem) {
    super(problem?.detail || String(status));
    this.status = status;
    this.problem = problem;
  }
}

function parseLinks(header) {
  const links = {};
  for (const match of (header || "").matchAll(/<([^>]+)>;\s*rel="(\w+)"/g)) {
    links[match[2]] = match[1];
  }
  return links;
}

export function offsetOf(link) {
  return Number(new URL(link, window.location.origin).searchParams.get("offset") ?? 0);
}

export async function get(path, params) {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== null && value !== undefined && value !== "") url.searchParams.set(key, value);
  }
  const response = await fetch(url.href, {method: "GET", headers: {Accept: "application/json"}, cache: "no-store", credentials: "same-origin"});
  if (response.status === 401) {
    session.expire();
    throw new ApiError(401, null);
  }
  if (!response.ok) {
    throw new ApiError(response.status, await response.json().catch(() => null));
  }
  const total = response.headers.get("X-Total-Count");
  return {
    data: await response.json(),
    total: total === null ? null : Number(total),
    links: parseLinks(response.headers.get("Link")),
  };
}

export function encode(segment) {
  return encodeURIComponent(segment);
}
