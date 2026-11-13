// §16 Review / §11 棚卸と証明 (Phase 17)。自分が Owner の資産の項目を判断する。
// 権限の棚卸 (access_review) は継続か取り消し、Owner の証明 (ownership_attestation) は証明か修正が必要か。
// 判断は既存の API (POST /reviews/items/{id}/decision) を呼ぶ。取り消しと修正が必要には理由が必須。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {encode, get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {decisionForm, operationButtons} from "../lib/forms.js";
import {PAGE_SIZE, asyncSection, formatDate, pager, table} from "../lib/components.js";

const COMMENT = {name: "comment", kind: "text", max_length: 1000};

export function reviewOperations(item) {
  const path = `/api/v1/reviews/items/${encode(item.id)}/decision`;
  const attestation = item.target_type === "asset";
  return [
    {id: attestation ? "attest" : "keep", method: "POST", path, body: {keep: true}, fields: [{...COMMENT, required: false}], step_up: true},
    {id: attestation ? "needs_fix" : "revoke", method: "POST", path, body: {keep: false}, fields: [{...COMMENT, required: true}], step_up: true},
  ];
}

export function reviewsPage(_segments, params) {
  const offset = Number(params.offset || 0);
  const status = h("p", {role: "status", class: "muted", id: "reviews-status"});
  const formArea = h("div", {id: "decision-area"});
  const listArea = h("div", {});

  function load() {
    listArea.replaceChildren(asyncSection(t("reviews.title"), async () => {
      const result = await get("/reviews/items", {status: "pending", limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("reviews.title"), [
        {title: t("reviews.kind"), render: (i) => t(i.target_type === "asset" ? "reviews.kind.attestation" : "reviews.kind.access")},
        {title: t("reviews.target"), render: (i) => `${i.target_type}: ${i.target_id}`},
        {title: t("reviews.asset"), render: (i) => h("a", {href: hrefFor(["assets", i.asset_id])}, i.asset_id)},
        {title: t("reviews.status"), render: (i) => i.status},
        {title: t("reviews.decided"), render: (i) => formatDate(i.decided_at)},
        {title: t("inbox.operations"), render: (i) => operationButtons(reviewOperations(i), (operation) => open(i, operation), i.target_id)},
      ], result.data), pager(result, (next) => navigate(["reviews"], {offset: next})));
    }));
  }

  function open(item, operation) {
    const opener = document.activeElement;
    const title = `${item.target_type}: ${item.target_id}`;
    formArea.replaceChildren(decisionForm({
      title, operation,
      onCancel: () => {
        formArea.replaceChildren();
        opener?.focus?.();
      },
      onDone: (done) => {
        formArea.replaceChildren();
        status.textContent = t("inbox.done", {item: title, op: t(`form.op.${done.id}`)});
        load();
      },
    }));
    formArea.querySelector("textarea, select, input, #decision-submit")?.focus();
  }

  load();
  return h("div", {}, h("h1", {}, t("reviews.title")), h("p", {class: "muted"}, t("reviews.description")), status, formArea, listArea);
}
