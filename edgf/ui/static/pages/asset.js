// §7 Data Catalog: 資産の詳細 (Data Product のページ)。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {ApiError, get} from "../lib/api.js";
import {hrefFor} from "../lib/router.js";
import {asyncSection, badge, certificationBadge, definitions, formatDate, formatRate, loading, problemView, section, sensitivityLabel, table, yesNo} from "../lib/components.js";
import {lineChart} from "../lib/charts.js";

const STATUS_KIND = {pass: "ok", fail: "bad", excepted: "warn"};

function metadata(asset) {
  return section(t("asset.metadata"), definitions([
    [t("asset.qualified_name"), asset.qualified_name],
    [t("asset.description"), asset.description || "-"],
    [t("asset.domain"), asset.domain || "-"],
    [t("catalog.data_class"), asset.data_class],
    [t("asset.cde"), yesNo(asset.is_cde)],
    [t("catalog.sensitivity"), sensitivityLabel(asset.sensitivity)],
    asset.effective_sensitivity ? [t("asset.effective_sensitivity"), sensitivityLabel(asset.effective_sensitivity)] : [t("asset.pii"), yesNo(asset.contains_pii)],
    [t("asset.tags"), asset.tags?.length ? h("span", {class: "tags"}, asset.tags.map((tag) => badge(tag, "neutral"))) : "-"],
  ]));
}

function ownership(asset) {
  const person = (value) => value || badge(t("asset.unassigned"), "bad");
  return section(t("asset.ownership"), definitions([
    [t("asset.owner"), person(asset.owner)], [t("asset.steward"), person(asset.steward)], [t("asset.custodian"), person(asset.custodian)],
  ]));
}

function schema(asset) {
  const columns = asset.schema || [];
  return section(t("asset.schema"), table(t("asset.schema"), [
    {title: t("asset.column"), render: (c) => h("code", {}, c.name)},
    {title: t("asset.type"), render: (c) => c.type},
    {title: t("asset.pii"), render: (c) => (c.pii ? badge(t("asset.pii"), "bad") : "-")},
    {title: t("asset.pii_category"), render: (c) => c.pii_category || "-"},
    {title: t("asset.purposes"), render: (c) => (c.processing_purposes || []).join(", ") || "-"},
    {title: t("asset.legal_basis"), render: (c) => c.legal_basis || "-"},
    {title: t("asset.column_description"), render: (c) => c.description || "-"},
  ], columns));
}

function retention(asset) {
  const until = asset.retention_days ? new Date(new Date(`${asset.created_at}Z`).getTime() + asset.retention_days * 86400000).toISOString() : null;
  return section(t("asset.retention"), definitions([
    [t("asset.lifecycle"), asset.lifecycle_state],
    [t("asset.retention_days"), asset.retention_days ? t("asset.days", {days: asset.retention_days}) : "-"],
    [t("asset.retention_until"), until ? until.slice(0, 10) : "-"],
    [t("asset.legal_hold"), asset.legal_hold ? badge(t("asset.legal_hold"), "warn") : yesNo(false)],
  ]));
}

async function terms(asset) {
  const {data} = await get("/glossary/terms", {limit: 1000});
  const linked = data.filter((term) => term.asset_ids.includes(asset.id));
  if (!linked.length) return h("p", {class: "muted"}, t("common.empty"));
  return h("ul", {}, linked.map((term) => h("li", {}, h("a", {href: hrefFor(["glossary", term.id])}, term.name))));
}

async function quality(asset) {
  const {data} = await get(`/quality/assets/${asset.id}/runs`, {limit: 30});
  if (!data.length) return h("p", {class: "muted"}, t("asset.quality_none"));
  const runs = [...data].reverse();
  const latest = data[0];
  return h("div", {},
    h("p", {}, `${t("asset.quality_latest")}: `, h("strong", {}, formatRate(latest.score)), ` (${formatDate(latest.executed_at)})`),
    lineChart(t("asset.quality"), runs.map((r) => formatDate(r.executed_at).slice(0, 10)), [{label: t("asset.score"), values: runs.map((r) => r.score)}]),
    table(t("asset.dimension"), [
      {title: t("asset.dimension"), render: ([name]) => name},
      {title: t("asset.score"), render: ([, score]) => formatRate(score)},
    ], Object.entries(latest.dimension_scores || {})));
}

async function policies(asset) {
  const {data} = await get(`/policies/evaluate/assets/${asset.id}`);
  return table(t("asset.policies"), [
    {title: t("asset.policy"), render: (r) => `${r.policy_id} ${r.name}`},
    {title: t("asset.severity"), render: (r) => r.severity},
    {title: t("asset.status"), render: (r) => badge(t(`policy.${r.status}`), STATUS_KIND[r.status] || "neutral")},
    {title: t("asset.message"), render: (r) => (r.status === "pass" ? "-" : r.message)},
  ], data.results);
}

async function latestVersion(contractId) {
  if (!contractId) return "-";
  const {data} = await get(`/mesh/contracts/${contractId}/versions`, {limit: 1000});
  const live = data.filter((v) => v.status !== "draft");
  return live.length ? `${live[live.length - 1].version} (${live[live.length - 1].status})` : "-";
}

async function products(asset) {
  const {data} = await get("/mesh/products", {asset_id: asset.id});
  const rows = [];
  for (const product of data) {
    for (const port of product.ports.filter((p) => p.asset_id === asset.id)) {
      rows.push({product, port, version: await latestVersion(port.contract_id)});
    }
  }
  return table(t("asset.products"), [
    {title: t("asset.product"), render: (r) => r.product.name},
    {title: t("asset.product_status"), render: (r) => r.product.status},
    {title: t("asset.port"), render: (r) => r.port.name},
    {title: t("asset.contract_version"), render: (r) => r.version},
  ], rows);
}

const CARD_FIELDS = {
  model: ["intended_use", "out_of_scope_use", "metrics", "bias_evaluation_ref", "constraints"],
  dataset: ["purpose", "collection_source", "license", "consent_purpose_id", "known_biases", "constraints"],
};

async function aiCard(asset) {
  const kind = asset.ai_kind === "model" ? "model" : "dataset";
  try {
    const {data} = await get(`/ai/assets/${asset.id}/${kind}-card`);
    const rows = CARD_FIELDS[kind].map((field) => {
      const value = data[field];
      return [t(`asset.card.${field}`), typeof value === "object" && value !== null ? JSON.stringify(value) : value || "-"];
    });
    return definitions([[t("asset.ai_kind"), asset.ai_kind], ...rows]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return definitions([[t("asset.ai_kind"), asset.ai_kind], [t("asset.ai_card"), t("asset.card_none")]]);
    }
    throw error;
  }
}

export function assetPage(segments) {
  const root = h("div", {}, h("h1", {}, t("catalog.title")), loading());
  get(`/assets/${encodeURIComponent(segments[0] || "")}`).then(({data: asset}) => {
    root.replaceChildren(
      h("p", {}, h("a", {href: "#/catalog"}, t("asset.back"))),
      h("div", {class: "page-head"}, h("h1", {}, asset.name), sensitivityLabel(asset.effective_sensitivity || asset.sensitivity), certificationBadge(asset.certification),
        h("a", {href: hrefFor(["lineage", asset.qualified_name])}, t("asset.lineage_link"))),
      h("div", {class: "grid"}, metadata(asset), ownership(asset), retention(asset), asyncSection(t("asset.terms"), () => terms(asset))),
      schema(asset),
      asyncSection(t("asset.quality"), () => quality(asset)),
      asyncSection(t("asset.policies"), () => policies(asset)),
      asyncSection(t("asset.products"), () => products(asset)),
      asset.ai_kind ? asyncSection(t("asset.ai_card"), () => aiCard(asset)) : null);
    document.title = `${asset.name} - ${t("app.title")}`;
  }).catch((error) => {
    if (!(error instanceof ApiError && error.status === 401)) root.replaceChildren(h("h1", {}, t("catalog.title")), problemView(error));
  });
  return root;
}
