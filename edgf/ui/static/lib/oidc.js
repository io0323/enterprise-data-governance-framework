// OIDC Authorization Code + PKCE。トークンはメモリにだけ持ち、ストレージは使わない。
// verifier と state もメモリだけに置くため、ログインはポップアップで行い、本体のページを残す。
// fetch の POST はトークンエンドポイントへの 1 件だけ (コードの交換に必須)。Control Plane の API は GET だけ。
const CHANNEL = "edgf-oidc";
let config = null;
let discovery = null;
let session = null;
let expiryTimer = null;
let onExpired = () => {};

function randomString(bytes = 32) {
  const data = crypto.getRandomValues(new Uint8Array(bytes));
  return base64Url(data);
}

function base64Url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function challengeOf(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function claimsOf(jwt) {
  const payload = jwt.split(".")[1].replaceAll("-", "+").replaceAll("_", "/");
  const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export async function initialize(expiredCallback) {
  onExpired = expiredCallback;
  const response = await fetch("./config.json", {method: "GET"});
  config = await response.json();
  return config.configured;
}

async function loadDiscovery() {
  if (discovery) return discovery;
  const url = `${config.issuer.replace(/\/$/, "")}/.well-known/openid-configuration`;
  const response = await fetch(url, {method: "GET"});
  if (!response.ok) throw new Error("discovery_failed");
  const document_ = await response.json();
  if (document_.issuer !== config.issuer) throw new Error("issuer_mismatch");
  discovery = document_;
  return discovery;
}

function redirectUri() {
  return new URL("./callback.html", window.location.href.split("#")[0]).href;
}

function waitForCode(state) {
  return new Promise((resolve, reject) => {
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (event) => {
      if (event.data.state !== state) return; // 別の試行の応答は無視する
      channel.close();
      if (event.data.error || !event.data.code) reject(new Error("login_failed"));
      else resolve(event.data.code);
    };
  });
}

function startSession(tokens) {
  const idClaims = claimsOf(tokens.id_token);
  const accessClaims = claimsOf(tokens.access_token);
  const lifetime = tokens.expires_in ?? (accessClaims.exp - Date.now() / 1000);
  session = {
    accessToken: tokens.access_token,
    idToken: tokens.id_token,
    name: idClaims.preferred_username || idClaims.sub,
  };
  clearTimeout(expiryTimer);
  expiryTimer = setTimeout(expire, Math.max(lifetime, 1) * 1000);
}

export async function login() {
  // ブラウザにブロックされないよう、クリックの処理の中で先に開く
  const popup = window.open("", "edgf-login", "popup,width=520,height=680");
  if (!popup) throw new Error("popup_blocked");
  const endpoints = await loadDiscovery();
  const verifier = randomString();
  const state = randomString(16);
  const nonce = randomString(16);
  const url = new URL(endpoints.authorization_endpoint);
  const params = {
    response_type: "code", client_id: config.client_id, redirect_uri: redirectUri(), scope: config.scopes.join(" "),
    state, nonce, code_challenge: await challengeOf(verifier), code_challenge_method: "S256",
  };
  if (config.audience) params.audience = config.audience;
  url.search = new URLSearchParams(params).toString();
  const pending = waitForCode(state);
  popup.location.href = url.href;
  const code = await pending;
  const response = await fetch(endpoints.token_endpoint, {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({
      grant_type: "authorization_code", code, redirect_uri: redirectUri(), client_id: config.client_id, code_verifier: verifier,
    }),
  });
  if (!response.ok) throw new Error("token_failed");
  const tokens = await response.json();
  const claims = claimsOf(tokens.id_token);
  if (claims.nonce !== nonce || claims.iss !== config.issuer) throw new Error("id_token_invalid");
  startSession(tokens);
}

export function expire() {
  clearTimeout(expiryTimer);
  session = null;
  onExpired();
}

export function logout() {
  const idToken = session?.idToken;
  clearTimeout(expiryTimer);
  session = null;
  const endpoint = discovery?.end_session_endpoint;
  if (endpoint && idToken) {
    const url = new URL(endpoint);
    url.search = new URLSearchParams({
      id_token_hint: idToken, client_id: config.client_id, post_logout_redirect_uri: new URL("./", window.location.href.split("#")[0]).href,
    }).toString();
    window.location.assign(url.href);
  } else {
    window.location.reload();
  }
}

export function accessToken() {
  return session?.accessToken ?? null;
}

export function userName() {
  return session?.name ?? null;
}
