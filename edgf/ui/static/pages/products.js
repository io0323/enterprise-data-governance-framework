// §15 Data Product の一覧: DATSIS の合否、Contract の版、利用登録の数。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {navigate} from "../lib/router.js";
import {PAGE_SIZE, asyncSection, badge, pager, table} from "../lib/components.js";

export function productsPage(_segments, params) {
  const offset = Number(params.offset || 0);
  return h("div", {}, h("h1", {}, t("products.title")), asyncSection(t("products.title"), async () => {
    const result = await get("/mesh/products/overview", {limit: PAGE_SIZE, offset});
    return h("div", {}, table(t("products.title"), [
      {title: t("products.name"), render: (p) => p.name},
      {title: t("products.domain"), render: (p) => p.domain},
      {title: t("products.owner"), render: (p) => p.owner},
      {title: t("products.status"), render: (p) => p.status},
      {title: t("products.datsis"), render: (p) => h("div", {}, badge(t(p.datsis_passed ? "products.pass" : "products.fail"), p.datsis_passed ? "ok" : "bad"),
        p.datsis_failed.length ? h("div", {class: "muted"}, t("products.failed", {list: p.datsis_failed.join(", ")})) : null)},
      {title: t("products.contracts"), render: (p) => p.contracts.map((c) => `${c.port}: ${c.version ?? "-"}${c.status ? ` (${c.status})` : ""}`).join(" / ") || "-"},
      {title: t("products.subscriptions"), render: (p) => p.subscription_count},
    ], result.data), pager(result, (next) => navigate(["products"], {offset: next})));
  }));
}
