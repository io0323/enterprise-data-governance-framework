// ハッシュ方式のルーティング。例: #/assets/abc?limit=20
export function parseHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  const segments = path.split("/").filter(Boolean).map(decodeURIComponent);
  return {segments, params: Object.fromEntries(new URLSearchParams(query))};
}

export function hrefFor(segments, params) {
  const path = segments.map(encodeURIComponent).join("/");
  const clean = Object.fromEntries(Object.entries(params || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false));
  const query = new URLSearchParams(clean).toString();
  return `#/${path}${query ? `?${query}` : ""}`;
}

export function navigate(segments, params) {
  window.location.hash = hrefFor(segments, params);
}
