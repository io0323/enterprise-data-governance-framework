// §16 Approval: 承認箱 (読み取り専用)。判断は API で行う。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {navigate} from "../lib/router.js";
import {PAGE_SIZE, asyncSection, formatDate, pager, table} from "../lib/components.js";

export function inboxPage(_segments, params) {
  const offset = Number(params.offset || 0);
  return h("div", {}, h("h1", {}, t("inbox.title")), h("p", {class: "muted"}, t("inbox.description")),
    asyncSection(t("inbox.title"), async () => {
      const result = await get("/approvals/inbox", {limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("inbox.title"), [
        {title: t("inbox.source"), render: (i) => i.source},
        {title: t("inbox.item"), render: (i) => i.title},
        {title: t("inbox.status"), render: (i) => i.status},
        {title: t("inbox.requested_by"), render: (i) => i.requested_by || "-"},
        {title: t("inbox.due"), render: (i) => formatDate(i.due_at)},
        {title: t("inbox.action"), render: (i) => h("code", {}, i.action)},
      ], result.data), pager(result, (next) => navigate(["inbox"], {offset: next})));
    }));
}
