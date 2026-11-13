// §16 Approval: 承認箱。各案件の操作は、承認箱のアダプタが返す既存の API を呼ぶ (Phase 17)。
// 認可と職務分離は API が判定する。UI は判断の入力と送信だけを行う。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {navigate} from "../lib/router.js";
import {decisionForm, operationButtons} from "../lib/forms.js";
import {PAGE_SIZE, asyncSection, formatDate, pager, table} from "../lib/components.js";

export function inboxPage(_segments, params) {
  const offset = Number(params.offset || 0);
  const status = h("p", {role: "status", class: "muted", id: "inbox-status"});
  const formArea = h("div", {id: "decision-area"});
  const listArea = h("div", {});

  function load() {
    listArea.replaceChildren(asyncSection(t("inbox.title"), async () => {
      const result = await get("/approvals/inbox", {limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("inbox.title"), [
        {title: t("inbox.source"), render: (i) => t(`inbox.kind.${i.source}`)},
        {title: t("inbox.item"), render: (i) => i.title},
        {title: t("inbox.status"), render: (i) => i.status},
        {title: t("inbox.requested_by"), render: (i) => i.requested_by || "-"},
        {title: t("inbox.due"), render: (i) => formatDate(i.due_at)},
        {title: t("inbox.operations"), render: (i) => operationButtons(i.operations || [], (operation) => open(i, operation), i.title)},
      ], result.data), pager(result, (next) => navigate(["inbox"], {offset: next})));
    }));
  }

  function open(item, operation) {
    const opener = document.activeElement;
    const close = () => {
      formArea.replaceChildren();
      opener?.focus?.();
    };
    formArea.replaceChildren(decisionForm({
      title: item.title, operation, onCancel: close,
      onDone: (done) => {
        formArea.replaceChildren();
        status.textContent = t("inbox.done", {item: item.title, op: t(`form.op.${done.id}`)});
        load();
        status.focus();
      },
    }));
    formArea.querySelector("textarea, select, input, #decision-submit")?.focus();
  }

  load();
  return h("div", {}, h("h1", {}, t("inbox.title")), h("p", {class: "muted"}, t("inbox.description")), status, formArea, listArea);
}
