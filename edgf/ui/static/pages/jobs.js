// §19 Observability: ジョブの状態。権限は API が判定する (権限が無ければ「権限がありません」)。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {asyncSection, badge, formatDate, table, yesNo} from "../lib/components.js";

export function jobsPage() {
  return h("div", {}, h("h1", {}, t("jobs.title")), asyncSection(t("jobs.title"), async () => {
    const {data} = await get("/ops/jobs");
    return table(t("jobs.title"), [
      {title: t("jobs.name"), render: (j) => h("code", {}, j.job)},
      {title: t("jobs.enabled"), render: (j) => yesNo(j.enabled)},
      {title: t("jobs.interval"), render: (j) => j.interval_seconds},
      {title: t("jobs.last_finished"), render: (j) => formatDate(j.last_finished_at)},
      {title: t("jobs.last_status"), render: (j) => (j.last_status ? badge(j.last_status, j.last_status === "succeeded" ? "ok" : "bad") : "-")},
      {title: t("jobs.last_count"), render: (j) => j.last_count ?? "-"},
      {title: t("jobs.failures"), render: (j) => j.consecutive_failures},
      {title: t("jobs.overdue"), render: (j) => (j.overdue ? badge(t("jobs.overdue"), "warn") : "-")},
    ], data);
  }));
}
