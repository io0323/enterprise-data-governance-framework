// §16 Issue の一覧 (閲覧できる資産のものだけ)。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {PAGE_SIZE, asyncSection, badge, formatDate, pager, section, table} from "../lib/components.js";

const STATUSES = ["open", "in_progress", "resolved", "closed"];
const SEVERITY_KIND = {critical: "bad", high: "bad", medium: "warn", low: "neutral"};

export function issuesPage(_segments, params) {
  const offset = Number(params.offset || 0);
  return h("div", {}, h("h1", {}, t("issues.title")),
    section(t("issues.show"), h("form", {class: "filters", onSubmit: (event) => {
      event.preventDefault();
      const status = new FormData(event.target).get("status");
      navigate(["issues"], status ? {status} : {});
    }},
    h("label", {}, t("issues.status"), h("select", {name: "status"}, h("option", {value: ""}, t("issues.any")), STATUSES.map((v) => h("option", {value: v, selected: v === params.status}, v)))),
    h("button", {type: "submit", class: "primary"}, t("issues.show")))),
    asyncSection(t("issues.title"), async () => {
      const result = await get("/issues", {status: params.status, limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("issues.title"), [
        {title: t("issues.name"), render: (i) => (i.asset_id ? h("a", {href: hrefFor(["assets", i.asset_id])}, i.title) : i.title)},
        {title: t("issues.kind"), render: (i) => i.kind},
        {title: t("issues.severity"), render: (i) => badge(i.severity, SEVERITY_KIND[i.severity] || "neutral")},
        {title: t("issues.status"), render: (i) => i.status},
        {title: t("issues.assignee"), render: (i) => i.assignee || "-"},
        {title: t("issues.level"), render: (i) => i.escalation_level},
        {title: t("issues.due"), render: (i) => formatDate(i.due_at)},
      ], result.data), pager(result, (next) => navigate(["issues"], {...params, offset: next})));
    }));
}
