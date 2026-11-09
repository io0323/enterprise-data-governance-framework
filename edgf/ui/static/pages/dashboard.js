// §17 KPI / §20 Maturity: ダッシュボード。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {asyncSection, badge, definitions, formatRate, section, table} from "../lib/components.js";
import {lineChart} from "../lib/charts.js";

// 設計書 §1.8 と §17 の目標。direction: at_least は値が目標以上、at_most は目標以下で達成
const TARGETS = [
  {metric: "owner_assignment_rate", target: 1, direction: "at_least", source: "§1.8 / §17"},
  {metric: "cde_quality_score_avg", target: 0.99, direction: "at_least", source: "§1.8 / §17"},
  {metric: "metadata_completeness", target: 0.95, direction: "at_least", source: "§17"},
  {metric: "freshness_rate", target: 0.98, direction: "at_least", source: "§17"},
  {metric: "compliance_rate", target: 1, direction: "at_least", source: "§17"},
  {metric: "mean_time_to_resolve_days", target: 30, direction: "at_most", days: true, source: "§1.8 / §17"},
];
const TREND_GROUPS = [["compliance_rate", "metadata_completeness", "owner_assignment_rate"], ["cde_quality_score_avg", "freshness_rate", "certified_rate"]];
const RATE_METRICS = new Set(["ai_training_approved_rate", "ai_card_complete_rate", "mesh_datsis_pass_rate", "quality_score_avg", "certified_rate"]);

function display(row, value) {
  if (value === null || value === undefined) return "-";
  return row.days ? t("kpi.days", {value}) : formatRate(value);
}

function stateBadge(row, value) {
  if (value === null || value === undefined) return badge(t("kpi.unknown"), "neutral");
  const met = row.direction === "at_least" ? value >= row.target : value <= row.target;
  return badge(t(met ? "kpi.met" : "kpi.missed"), met ? "ok" : "bad");
}

function usageRow(kpi, history) {
  const values = history.map((r) => r.metrics.governance_activity_assets_30d).filter((v) => v !== undefined);
  let state = badge(t("kpi.unknown"), "neutral");
  if (values.length > 1) {
    const rising = values[values.length - 1] > values[0];
    state = badge(t(rising ? "kpi.trend_up" : "kpi.trend_flat"), rising ? "ok" : "warn");
  }
  return [t("kpi.usage"), kpi.governance_activity_assets_30d, t("kpi.trend_up"), state, "§17"];
}

function targetsTable(kpi, history) {
  const rows = TARGETS.map((row) => [t(`kpi.${row.metric}`), display(row, kpi[row.metric]),
    `${row.direction === "at_least" ? "≥" : "≤"} ${row.days ? t("kpi.days", {value: row.target}) : formatRate(row.target)}`, stateBadge(row, kpi[row.metric]), row.source]);
  rows.push([t("kpi.catalog_registration"), "-", "≥ 95.0%", h("span", {}, badge(t("kpi.unknown"), "neutral"), " ", t("kpi.catalog_registration_note")), "§1.8"]);
  rows.push(usageRow(kpi, history));
  return table(t("kpi.targets"), ["kpi.metric", "kpi.value", "kpi.target", "kpi.state", "kpi.source"].map((key, i) => ({title: t(key), render: (r) => r[i]})), rows);
}

function maturityView(kpi) {
  const maturity = kpi.maturity;
  const next = maturity.next_level;
  return h("div", {},
    definitions([[t("maturity.level"), `${maturity.indicative_level} ${t(`maturity.name.${maturity.indicative_level}`)}`]]),
    next ? h("div", {},
      h("p", {}, h("strong", {}, t("maturity.next", {level: next.level, name: t(`maturity.name.${next.level}`)}))),
      table(t("maturity.condition"), [
        {title: t("maturity.condition"), render: (c) => `${t(`kpi.${c.metric}`)} ${c.minimum === null ? t("maturity.measured") : `≥ ${formatRate(c.minimum)}`}`},
        {title: t("maturity.current"), render: (c) => formatRate(c.value)},
        {title: t("kpi.state"), render: (c) => badge(t(c.met ? "kpi.met" : "kpi.missed"), c.met ? "ok" : "bad")},
      ], next.conditions)) : h("p", {}, t("maturity.top")),
    h("p", {class: "muted"}, maturity.note));
}

function group(title, kpi, metrics) {
  return section(title, definitions(metrics.map((m) => [t(`kpi.${m}`), RATE_METRICS.has(m) ? formatRate(kpi[m]) : kpi[m] ?? "-"])));
}

function trends(history) {
  if (!history.length) return h("p", {class: "muted"}, t("kpi.trend_none"));
  const labels = history.map((r) => r.day);
  return h("div", {class: "grid"}, TREND_GROUPS.map((metrics) => h("div", {},
    lineChart(metrics.map((m) => t(`kpi.${m}`)).join(" / "), labels, metrics.map((m) => ({label: t(`kpi.${m}`), values: history.map((r) => r.metrics[m] ?? null)}))),
    h("ul", {class: "muted"}, metrics.map((m) => h("li", {}, t(`kpi.${m}`)))))));
}

function domainTable(rows) {
  if (!rows.length) return h("p", {class: "muted"}, t("kpi.trend_none"));
  const latest = rows.reduce((max, r) => (r.day > max ? r.day : max), rows[0].day);
  const current = rows.filter((r) => r.day === latest);
  return h("div", {}, h("p", {class: "muted"}, t("kpi.snapshot_day", {day: latest})),
    table(t("kpi.domains"), [
      {title: t("kpi.domain"), render: (r) => r.scope},
      {title: t("kpi.asset_count"), render: (r) => r.metrics.asset_count},
      ...["owner_assignment_rate", "cde_quality_score_avg", "metadata_completeness", "freshness_rate", "compliance_rate"].map((m) => ({title: t(`kpi.${m}`), render: (r) => formatRate(r.metrics[m])})),
      {title: t("kpi.open_issue_count"), render: (r) => r.metrics.open_issue_count},
    ], current));
}

export function dashboardPage() {
  const body = h("div", {});
  const data = Promise.all([get("/kpi"), get("/kpi/history", {scope: "enterprise", limit: 1000})]).then(([kpi, history]) => ({kpi: kpi.data, history: history.data}));
  body.append(
    asyncSection(t("kpi.targets"), async () => {
      const {kpi, history} = await data;
      return targetsTable(kpi, history);
    }),
    asyncSection(t("maturity.title"), async () => maturityView((await data).kpi)),
    asyncSection(t("kpi.trend"), async () => trends((await data).history)),
    asyncSection(t("kpi.domains"), async () => domainTable((await get("/kpi/history", {scope: "domains", limit: 1000})).data)),
    asyncSection(t("kpi.title"), async () => {
      const {kpi} = await data;
      return h("div", {class: "grid"},
        group(t("kpi.ai"), kpi, ["ai_asset_count", "ai_training_approved_rate", "ai_remediation_pending_count", "ai_card_complete_rate", "ai_inheritance_violation_count"]),
        group(t("kpi.mesh"), kpi, ["mesh_product_published_count", "mesh_datsis_pass_rate", "mesh_breaking_rejected_count", "mesh_subscription_active_count", "mesh_schema_drift_issue_count"]),
        group(t("kpi.privacy"), kpi, ["privacy_pii_asset_count", "privacy_erasure_open_count", "privacy_erasure_overdue_count"]));
    }));
  return h("div", {}, h("h1", {}, t("kpi.title")), body);
}
