// §16 Issue の一覧 (閲覧できる資産のものだけ)。Phase 17 から状態と担当者を更新できる (既存の PATCH /issues/{id})。
// 更新できるかは API が判定する (Committee、admin、資産の Owner / Steward、担当者)。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {encode, get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {decisionForm, operationButtons} from "../lib/forms.js";
import {PAGE_SIZE, asyncSection, badge, formatDate, pager, section, table} from "../lib/components.js";

const STATUSES = ["open", "in_progress", "resolved", "closed"];
const SEVERITY_KIND = {critical: "bad", high: "bad", medium: "warn", low: "neutral"};

export function issueOperation(issue) {
  return {id: "update", method: "PATCH", path: `/api/v1/issues/${encode(issue.id)}`, body: {}, step_up: false, fields: [
    {name: "status", kind: "select", options: STATUSES, required: false},
    {name: "assignee", kind: "text", max_length: 200, required: false},
    {name: "comment", kind: "text", max_length: 1000, required: false},
  ]};
}

export function issuesPage(_segments, params) {
  const offset = Number(params.offset || 0);
  const status = h("p", {role: "status", class: "muted", id: "issues-status"});
  const formArea = h("div", {id: "decision-area"});
  const listArea = h("div", {});

  function load() {
    listArea.replaceChildren(asyncSection(t("issues.title"), async () => {
      const result = await get("/issues", {status: params.status, limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("issues.title"), [
        {title: t("issues.name"), render: (i) => (i.asset_id ? h("a", {href: hrefFor(["assets", i.asset_id])}, i.title) : i.title)},
        {title: t("issues.kind"), render: (i) => i.kind},
        {title: t("issues.severity"), render: (i) => badge(i.severity, SEVERITY_KIND[i.severity] || "neutral")},
        {title: t("issues.status"), render: (i) => i.status},
        {title: t("issues.assignee"), render: (i) => i.assignee || "-"},
        {title: t("issues.level"), render: (i) => i.escalation_level},
        {title: t("issues.due"), render: (i) => formatDate(i.due_at)},
        {title: t("inbox.operations"), render: (i) => operationButtons([issueOperation(i)], (operation) => open(i, operation), i.title, i.id)},
      ], result.data), pager(result, (next) => navigate(["issues"], {...params, offset: next})));
    }));
  }

  function open(issue, operation) {
    const opener = document.activeElement;
    formArea.replaceChildren(decisionForm({
      title: issue.title, operation,
      onCancel: () => {
        formArea.replaceChildren();
        opener?.focus?.();
      },
      onDone: () => {
        formArea.replaceChildren();
        status.textContent = t("issues.updated", {title: issue.title});
        load();
      },
    }));
    formArea.querySelector("select, textarea, input")?.focus();
  }

  load();
  return h("div", {}, h("h1", {}, t("issues.title")), status, formArea,
    section(t("issues.show"), h("form", {class: "filters", onSubmit: (event) => {
      event.preventDefault();
      const status = new FormData(event.target).get("status");
      navigate(["issues"], status ? {status} : {});
    }},
    h("label", {}, t("issues.status"), h("select", {name: "status"}, h("option", {value: ""}, t("issues.any")), STATUSES.map((v) => h("option", {value: v, selected: v === params.status}, v)))),
    h("button", {type: "submit", class: "primary"}, t("issues.show")))),
    listArea);
}
