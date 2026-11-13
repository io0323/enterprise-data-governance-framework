// ログイン中の利用者 (BFF。Phase 17)。セッションは HttpOnly の Cookie にあり、JavaScript からは読めない。
// ここが持つのは GET /ui/session の応答 (利用者の表示名と CSRF のトークン) だけで、メモリにだけ置く。
let current = null;
let onExpired = () => {};

export function onExpire(callback) {
  onExpired = callback;
}

export async function load() {
  const response = await fetch("/ui/session", {method: "GET", headers: {Accept: "application/json"}, cache: "no-store", credentials: "same-origin"});
  current = response.ok ? await response.json() : null;
  return current;
}

export function user() {
  return current;
}

export function csrfToken() {
  return current?.csrf_token ?? null;
}

export function expire() {
  current = null;
  onExpired();
}

// ログインはサーバへの画面遷移 (IdP へ転送される)。戻り先は今の画面
export function login({maxAge = null} = {}) {
  const params = new URLSearchParams({return_to: window.location.hash});
  if (maxAge !== null) params.set("max_age", String(maxAge));
  window.location.assign(`/ui/auth/login?${params.toString()}`);
}
