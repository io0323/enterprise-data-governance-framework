// 共通の部品: 表、ページング、バッジ、Problem Details の表示。
import {h} from "./dom.js";
import {t} from "./i18n.js";
import {ApiError, offsetOf} from "./api.js";

export const PAGE_SIZE = 25;

export function loading() {
  return h("p", {class: "muted", role: "status"}, t("common.loading"));
}

export function formatDate(value) {
  if (!value) return "-";
  const date = new Date(`${value}`.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString().slice(0, 16).replace("T", " ");
}

export function formatRate(value) {
  return value === null || value === undefined ? "-" : `${(value * 100).toFixed(1)}%`;
}

export function yesNo(value) {
  return value ? t("common.yes") : t("common.no");
}

export function badge(text, kind) {
  return h("span", {class: `badge badge-${kind}`}, text);
}

export function sensitivityLabel(value) {
  return badge(t(`sensitivity.${value}`), `sensitivity-${value}`);
}

export function certificationBadge(value) {
  if (value === "certified") return badge(t("certification.certified"), "certified");
  if (value === "deprecated") return badge(t("certification.deprecated"), "deprecated");
  return badge(t("certification.draft"), "draft");
}

export function section(title, ...children) {
  return h("section", {class: "panel"}, h("h2", {}, title), ...children);
}

export function definitions(rows) {
  return h("dl", {class: "definitions"}, rows.map(([label, value]) => [h("dt", {}, label), h("dd", {}, value ?? "-")]));
}

/** columns: [{title, render(row)}]。caption は支援技術向けに必ず付ける。 */
export function table(caption, columns, rows) {
  if (!rows.length) return h("p", {class: "muted"}, t("common.empty"));
  return h("div", {class: "table-wrap", tabindex: "0", role: "region", "aria-label": caption},
    h("table", {},
      h("caption", {class: "visually-hidden"}, caption),
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", {scope: "col"}, c.title)))),
      h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((c) => h("td", {}, c.render(row))))))));
}

/** X-Total-Count と Link ヘッダからページングの操作を作る。 */
export function pager(result, onOffset) {
  const prev = result.links.prev;
  const next = result.links.next;
  return h("nav", {class: "pager", "aria-label": t("common.pagination")},
    h("button", {type: "button", disabled: !prev, onClick: () => onOffset(offsetOf(prev))}, t("common.prev")),
    h("span", {role: "status"}, t("common.total", {count: result.total ?? result.data.length})),
    h("button", {type: "button", disabled: !next, onClick: () => onOffset(offsetOf(next))}, t("common.next")));
}

/** Problem Details の表示。403 は「権限がありません」。 */
export function problemView(error) {
  if (error instanceof ApiError && error.status === 403) {
    return h("div", {class: "notice notice-denied", role: "alert"},
      h("strong", {}, t("error.forbidden")),
      error.problem?.detail ? h("p", {}, error.problem.detail) : null,
      requestId(error));
  }
  if (error instanceof ApiError) {
    return h("div", {class: "notice notice-error", role: "alert"},
      h("strong", {}, error.problem?.title || t("error.generic")),
      error.problem?.detail ? h("p", {}, error.problem.detail) : null,
      requestId(error));
  }
  return h("div", {class: "notice notice-error", role: "alert"}, h("strong", {}, t("error.generic")));
}

function requestId(error) {
  return error.problem?.request_id ? h("p", {class: "muted"}, t("error.request_id", {id: error.problem.request_id})) : null;
}

/** 非同期の取得を 1 つの領域に表示する。失敗してもページ全体は壊さない。 */
export function asyncSection(title, load) {
  const body = h("div", {}, loading());
  load().then((content) => body.replaceChildren(content)).catch((error) => {
    if (error instanceof ApiError && error.status === 401) return;
    body.replaceChildren(problemView(error));
  });
  return section(title, body);
}
