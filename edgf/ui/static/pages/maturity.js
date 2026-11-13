// §20 ドメイン別の成熟度の評価 (Phase 18)。
// 「正式な評価」は Committee の判定、「目安」は KPI による自動の判定。画面では必ず分けて表示する。
// 自己評価はドメインの Owner が入力し、actions.js から送る (CSRF のトークンを付ける)。判定は承認箱から行う。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {ApiError, encode, get} from "../lib/api.js";
import * as actions from "../lib/actions.js";
import {hrefFor} from "../lib/router.js";
import {asyncSection, badge, definitions, formatDate, section, table} from "../lib/components.js";

function levelText(level) {
  return level === null || level === undefined ? "-" : `${level} ${t(`maturity.name.${level}`)}`;
}

function formal(level) {
  return h("span", {class: "maturity-formal"}, badge(t("maturity_eval.formal"), "certified"), " ", levelText(level));
}

function indicative(level) {
  return h("span", {class: "maturity-indicative"}, badge(t("maturity_eval.indicative"), "neutral"), " ", levelText(level));
}

function enterpriseView() {
  return asyncSection(t("maturity_eval.enterprise"), async () => {
    const [{data}, kpi] = await Promise.all([get("/maturity/enterprise"), get("/kpi")]);
    return h("div", {},
      definitions([
        [t("maturity_eval.minimum"), formal(data.minimum_level)],
        [t("maturity_eval.kpi_indicative"), indicative(kpi.data.maturity.indicative_level)],
        [t("maturity_eval.unevaluated"), String(data.unevaluated_count)],
      ]),
      table(t("maturity_eval.distribution"), [
        {title: t("maturity_eval.level"), render: ([level]) => levelText(Number(level))},
        {title: t("maturity_eval.domain_count"), render: ([, count]) => String(count)},
      ], Object.entries(data.distribution)),
      table(t("maturity_eval.domains"), [
        {title: t("maturity_eval.domain"), render: (d) => h("a", {href: hrefFor(["maturity", d.domain])}, d.domain)},
        {title: t("maturity_eval.formal"), render: (d) => (d.evaluated ? formal(d.level) : t("maturity_eval.not_evaluated"))},
        {title: t("maturity_eval.decided_at"), render: (d) => formatDate(d.decided_at)},
        {title: t("maturity_eval.reference"), render: (d) => (d.reference ? badge(t("maturity_eval.reference"), "certified") : "-")},
      ], data.domains),
      h("p", {class: "muted"}, t("maturity_eval.note")));
  });
}

function gapsView(gaps) {
  if (!gaps.length) return h("p", {class: "muted"}, t("maturity_eval.no_gaps"));
  return table(t("maturity_eval.gaps"), [
    {title: t("maturity_eval.level"), render: (g) => String(g.level)},
    {title: t("maturity_eval.criterion"), render: (g) => g.id},
    {title: t("maturity_eval.kind"), render: (g) => t(`maturity_eval.kind.${g.kind}`)},
    {title: t("maturity_eval.detail"), render: (g) => (g.kind === "automatic" ? `${g.metric} (${t(`maturity_eval.reason.${g.reason}`)})` : g.description)},
  ], gaps);
}

function isHttps(value) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function selfAssessmentForm(assessment, status, onDone) {
  const manual = (assessment.criteria.levels || []).flatMap((lv) => lv.manual.map((m) => ({...m, level: lv.level})));
  const previous = Object.fromEntries((assessment.self_assessment || []).map((i) => [i.criterion_id, i]));
  const error = h("p", {class: "form-error", role: "alert"});
  const rows = manual.map((m) => {
    const before = previous[m.id];
    const achieved = h("input", {type: "checkbox", id: `achieved-${m.id}`, name: `achieved-${m.id}`, checked: Boolean(before?.achieved)});
    const uri = h("input", {type: "url", id: `uri-${m.id}`, name: `uri-${m.id}`, placeholder: t("maturity_eval.uri_placeholder"), value: before?.evidence?.[0]?.uri || ""});
    const description = h("textarea", {id: `description-${m.id}`, name: `description-${m.id}`, rows: "2", maxlength: "1000"});
    description.value = before?.evidence?.[0]?.description || "";
    return {m, achieved, uri, description, element: h("fieldset", {class: "criterion"},
      h("legend", {}, `Level ${m.level} ${m.id}: ${m.description}`),
      h("p", {class: "muted"}, t("maturity_eval.evidence_needed", {evidence: m.evidence})),
      h("label", {class: "inline", for: achieved.id}, achieved, t("maturity_eval.achieved")),
      h("div", {class: "field"}, h("label", {for: uri.id}, t("maturity_eval.evidence_uri")), uri),
      h("div", {class: "field"}, h("label", {for: description.id}, t("maturity_eval.evidence_description")), description))};
  });
  const submit = h("button", {type: "submit", class: "primary", id: "self-assessment-submit"}, t("maturity_eval.submit"));
  return h("form", {class: "decision-form panel", id: "self-assessment-form", novalidate: true, onSubmit: async (event) => {
    event.preventDefault();
    error.textContent = "";
    const items = [];
    for (const row of rows) {
      const uri = row.uri.value.trim();
      if (uri && !isHttps(uri)) {
        error.textContent = t("maturity_eval.https_only");
        row.uri.focus();
        return;
      }
      const evidence = uri ? [{uri, description: row.description.value.trim()}] : [];
      items.push({criterion_id: row.m.id, achieved: row.achieved.checked, evidence});
    }
    submit.disabled = true;
    try {
      await actions.call("PUT", `/api/v1/maturity/assessments/${encode(assessment.id)}/self-assessment`, {items});
      onDone();
      status.textContent = t("maturity_eval.submitted");
    } catch (failure) {
      submit.disabled = false;
      if (failure instanceof ApiError && failure.status === 401) return;
      error.textContent = failure instanceof ApiError ? (failure.problem?.detail || t("error.generic")) : t("error.generic");
    }
  }},
  h("h2", {}, t("maturity_eval.self_assessment")), h("p", {class: "muted"}, t("maturity_eval.self_assessment_note")),
  rows.map((r) => r.element), error, h("div", {class: "form-actions"}, submit));
}

function domainView(name) {
  const area = h("div", {});
  // 結果の表示は読み直す領域の外に置く (提出のあとに画面を読み直しても残す)
  const status = h("p", {role: "status", class: "muted", id: "self-assessment-status"});
  function load() {
    area.replaceChildren(asyncSection(t("maturity_eval.domain_title", {domain: name}), async () => {
      const {data} = await get(`/maturity/domains/${encode(name)}`);
      const latest = data.latest;
      const parts = [
        definitions([
          [t("maturity_eval.formal"), data.evaluated ? formal(data.level) : t("maturity_eval.not_evaluated")],
          [t("maturity_eval.indicative"), indicative(data.indicative_level)],
          [t("maturity_eval.target"), data.target_level === null ? "-" : levelText(data.target_level)],
          [t("maturity_eval.latest_status"), latest ? t(`maturity_eval.status.${latest.status}`) : "-"],
          [t("maturity_eval.cap"), latest?.level_cap ?? "-"],
        ]),
        h("h3", {}, t("maturity_eval.gaps")), gapsView(data.next_level_gaps),
        h("h3", {}, t("maturity_eval.references")),
        data.references.length ? table(t("maturity_eval.references"), [
          {title: t("maturity_eval.domain"), render: (r) => h("a", {href: hrefFor(["maturity", r.domain])}, r.domain)},
          {title: t("maturity_eval.formal"), render: (r) => formal(r.level)},
          {title: t("maturity_eval.reason_label"), render: (r) => r.reason || "-"},
        ], data.references) : h("p", {class: "muted"}, t("maturity_eval.no_references")),
        h("h3", {}, t("maturity_eval.history")),
        table(t("maturity_eval.history"), [
          {title: t("maturity_eval.created"), render: (a) => formatDate(a.created_at)},
          {title: t("maturity_eval.status_label"), render: (a) => t(`maturity_eval.status.${a.status}`)},
          {title: t("maturity_eval.indicative"), render: (a) => levelText(a.indicative_level)},
          {title: t("maturity_eval.cap"), render: (a) => a.level_cap ?? "-"},
          {title: t("maturity_eval.formal"), render: (a) => levelText(a.decided_level)},
          {title: t("maturity_eval.decided_at"), render: (a) => formatDate(a.decided_at)},
        ], data.history),
      ];
      if (latest && ["auto_evaluated", "self_assessed"].includes(latest.status)) parts.push(selfAssessmentForm(latest, status, load));
      if (latest?.status === "self_assessed") parts.push(h("p", {class: "muted"}, t("maturity_eval.decide_in_inbox")));
      return h("div", {}, ...parts);
    }));
  }
  load();
  return h("div", {}, status, area);
}

export function maturityPage(segments) {
  const name = segments[0];
  if (name) {
    return h("div", {}, h("h1", {}, t("maturity_eval.domain_title", {domain: name})),
      h("p", {}, h("a", {href: hrefFor(["maturity"])}, t("maturity_eval.back"))), domainView(name));
  }
  return h("div", {}, h("h1", {}, t("maturity_eval.title")), h("p", {class: "muted"}, t("maturity_eval.description")),
    section(t("maturity_eval.legend"), h("p", {}, indicative(null), " ", t("maturity_eval.legend_indicative")),
      h("p", {}, formal(null), " ", t("maturity_eval.legend_formal"))),
    enterpriseView());
}
