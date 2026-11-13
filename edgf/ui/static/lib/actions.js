// 書き込みの操作 (Phase 17)。GET 以外の fetch はこのファイルからだけ送る (静的な検査で確かめる)。
// すべての要求に X-CSRF-Token を付ける。サーバは Origin / Sec-Fetch-Site とトークンを確かめる。
import * as session from "./session.js";
import {ApiError} from "./api.js";

const METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function send(method, path, body) {
  if (!METHODS.has(method)) throw new Error(`unsupported method ${method}`);
  const token = session.csrfToken();
  if (!token) throw new ApiError(401, null);
  const response = await fetch(path, {
    method,
    headers: {"Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": token},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
  });
  const problem = response.ok ? null : await response.json().catch(() => null);
  if (response.status === 401 && problem?.type?.endsWith("step-up-required")) {
    session.login({maxAge: 0}); // 再ログインして、元の画面に戻る
    throw new ApiError(401, problem);
  }
  if (response.status === 401) {
    session.expire();
    throw new ApiError(401, problem);
  }
  if (!response.ok) throw new ApiError(response.status, problem);
  return response.status === 204 ? null : response.json().catch(() => null);
}

// API の呼び出し。path は /api/v1 から始まるサーバの既存の API (承認箱のアダプタが返す値)
export function call(method, path, body) {
  if (!path.startsWith("/api/v1/")) throw new Error("API の外へは送らない");
  return send(method, path, body);
}

// ログアウト。サーバがセッションを消し、IdP のログアウトの URL (end_session_endpoint があれば) を返す
export async function logout() {
  const result = await send("POST", "/ui/auth/logout");
  window.location.assign(result?.logout_url || "/ui/");
}
