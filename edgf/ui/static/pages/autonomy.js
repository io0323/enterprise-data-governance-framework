// §20 Level 5 自律統治 (Phase 19。docs/adr/0001)。
// 読み取り: 全体の状態、プレイブック、実行の記録 (dry_run の結果を含む)、品質の異常、分類の提案。
// 書き込み: 停止 (すぐに効く)、再開 (✓)、区分 A の取り消し、分類の提案の確定と却下 (✓)。送信は lib/actions.js だけが行う (CSRF のトークンを付ける)。
// ✓ の操作 (再開、確定と却下) は、ログインから時間がたっていると再ログインを求める (ステップアップ)。認可は API が判定する。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import * as session from "../lib/session.js";
import {decisionForm, operationButtons} from "../lib/forms.js";
import {asyncSection, badge, definitions, formatDate, formatRate, table} from "../lib/components.js";

const API = "/api/v1/autonomy";
const REASON = {name: "reason", kind: "text", required: true, max_length: 1000};
const OPTIONAL_REASON = {name: "reason", kind: "text", required: false, max_length: 1000};

function hasRole(...roles) {
  const user = session.user();
  return Boolean(user && user.roles.some((r) => roles.includes(r)));
}

function stopOperation() {
  return {id: "stop", method: "POST", path: `${API}/stop`, body: {}, fields: [REASON], step_up: false};
}

function resumeOperation(playbook = null) {
  return {id: "resume", method: "POST", path: `${API}/resume`, body: playbook ? {playbook} : {}, fields: [REASON], step_up: true};
}

function undoOperation(run) {
  // 閲覧の承認や Certification を戻す取り消しは、承認行為と同じくステップアップを求める (サーバの undo_step_up)
  return {id: "undo", method: "POST", path: `${API}/runs/${encodeURIComponent(run.id)}/undo`, body: {}, fields: [REASON],
    step_up: Boolean(run.undo_step_up)};
}

function suggestionOperations(s) {
  const path = `${API}/suggestions/${encodeURIComponent(s.id)}/decision`;
  return [
    {id: "adopt", method: "POST", path, body: {accept: true}, fields: [OPTIONAL_REASON], step_up: true},
    {id: "reject", method: "POST", path, body: {accept: false}, fields: [REASON], step_up: true},
  ];
}

function modeBadge(mode) {
  return badge(t(`autonomy.mode.${mode}`), mode === "active" ? "certified" : "neutral");
}

function categoryBadge(category) {
  return badge(t(`autonomy.category.${category}`), category === "A" ? "warn" : "neutral");
}

function summary(value) {
  if (value === null || value === undefined) return "-";
  return Object.entries(value).map(([k, v]) => `${k}=${typeof v === "object" && v !== null ? JSON.stringify(v) : v}`).join(", ") || "-";
}

function anomalyEvidence(a) {
  const e = a.evidence || {};
  if (a.kind === "trend") return t("autonomy.anomaly.trend_detail", {slope: e.slope_per_day, days: e.days_until, threshold: e.threshold});
  return t("autonomy.anomaly.robust_detail", {value: e.value, median: e.median, mad: e.mad, z: e.z === null ? "-" : e.z});
}

function evidenceText(s) {
  return (s.evidence || []).map((e) => {
    if (e.source === "lineage") return t("autonomy.evidence.lineage", {upstream: `${e.upstream}.${e.upstream_column}`});
    if (e.source === "glossary") return t("autonomy.evidence.glossary", {term: e.term, rule: e.rule_id});
    return t("autonomy.evidence.rule", {rule: e.rule_id, pattern: e.pattern, field: e.field});
  }).join(" / ");
}

export function autonomyPage() {
  const status = h("p", {role: "status", class: "muted", id: "autonomy-status", tabindex: "-1"});
  const formArea = h("div", {id: "decision-area"});
  const area = h("div", {});

  function open(title, operation) {
    const opener = document.activeElement;
    const close = () => {
      formArea.replaceChildren();
      opener?.focus?.();
    };
    formArea.replaceChildren(decisionForm({
      title, operation, onCancel: close,
      onDone: (done) => {
        formArea.replaceChildren();
        status.textContent = t("autonomy.done", {item: title, op: t(`form.op.${done.id}`)});
        load();
        status.focus();
      },
    }));
    formArea.querySelector("textarea, select, input, #decision-submit")?.focus();
  }

  function statusSection(data) {
    const buttons = [];
    if (!data.stopped && hasRole("committee", "admin")) buttons.push(stopOperation());
    if (data.stopped && hasRole("committee")) buttons.push(resumeOperation());
    return h("div", {},
      definitions([
        [t("autonomy.enabled"), data.enabled ? badge(t("autonomy.on"), "certified") : badge(t("autonomy.off"), "neutral")],
        [t("autonomy.state"), data.stopped ? badge(t("autonomy.stopped"), "bad") : badge(t("autonomy.running"), "certified")],
        [t("autonomy.reason"), data.reason || "-"],
        [t("autonomy.changed"), data.changed_at ? `${formatDate(data.changed_at)} (${data.changed_by})` : "-"],
        [t("autonomy.last_detection"), formatDate(data.last_detection_at)],
      ]),
      buttons.length ? operationButtons(buttons, (op) => open(t("autonomy.title"), op), t("autonomy.state"), "control") : null,
      h("h2", {}, t("autonomy.playbooks")),
      table(t("autonomy.playbooks"), [
        {title: t("autonomy.playbook"), render: (p) => p.id},
        {title: t("autonomy.action"), render: (p) => p.action},
        {title: t("autonomy.category"), render: (p) => categoryBadge(p.category)},
        {title: t("autonomy.mode"), render: (p) => modeBadge(p.mode)},
        {title: t("autonomy.limit"), render: (p) => String(p.max_actions_per_run)},
        {title: t("autonomy.false_positive"), render: (p) => (p.false_positive.rate === null ? "-" : formatRate(p.false_positive.rate))},
        {title: t("autonomy.state"), render: (p) => (p.suspended
          ? h("span", {}, badge(t("autonomy.suspended"), "bad"), " ", p.suspended_reason,
            hasRole("committee") ? operationButtons([resumeOperation(p.id)], (op) => open(p.id, op), p.id, `playbook-${p.id}`) : null)
          : t("autonomy.running"))},
      ], data.playbooks),
      data.playbooks.length ? null : h("p", {class: "muted"}, t("autonomy.no_playbooks")));
  }

  function runsSection(runs) {
    return table(t("autonomy.runs"), [
      {title: t("autonomy.created"), render: (r) => formatDate(r.created_at)},
      {title: t("autonomy.playbook"), render: (r) => r.playbook_id},
      {title: t("autonomy.action"), render: (r) => r.action},
      {title: t("autonomy.mode"), render: (r) => modeBadge(r.mode)},
      {title: t("autonomy.run_status"), render: (r) => t(`autonomy.status.${r.status}`)},
      {title: t("autonomy.target"), render: (r) => `${r.target_type}: ${r.target_id}`},
      {title: t("autonomy.change"), render: (r) => `${summary(r.before)} → ${summary(r.after)}`},
      {title: t("autonomy.operations"), render: (r) => (r.undoable
        ? operationButtons([undoOperation(r)], (op) => open(`${r.playbook_id} ${r.action}`, op), r.id, r.id) : "-")},
    ], runs);
  }

  function anomaliesSection(anomalies) {
    return table(t("autonomy.anomalies"), [
      {title: t("autonomy.detected"), render: (a) => formatDate(a.detected_at)},
      {title: t("autonomy.asset"), render: (a) => a.qualified_name || a.asset_id},
      {title: t("autonomy.kind"), render: (a) => t(`autonomy.anomaly.${a.kind}`)},
      {title: t("autonomy.metric"), render: (a) => a.metric},
      {title: t("autonomy.evidence"), render: (a) => anomalyEvidence(a)},
    ], anomalies);
  }

  function suggestionsSection(suggestions) {
    return table(t("autonomy.suggestions"), [
      {title: t("autonomy.asset"), render: (s) => `${s.qualified_name}.${s.column}`},
      {title: t("autonomy.proposal"), render: (s) => summary(s.proposal)},
      {title: t("autonomy.current"), render: (s) => summary(s.current)},
      {title: t("autonomy.confidence"), render: (s) => formatRate(s.confidence)},
      {title: t("autonomy.evidence"), render: (s) => evidenceText(s)},
      {title: t("autonomy.run_status"), render: (s) => t(`autonomy.suggestion.${s.status}`)},
      {title: t("autonomy.operations"), render: (s) => (s.status === "proposed"
        ? operationButtons(suggestionOperations(s), (op) => open(`${s.qualified_name}.${s.column}`, op), s.column, s.id) : "-")},
    ], suggestions);
  }

  function load() {
    area.replaceChildren(
      asyncSection(t("autonomy.overview"), async () => statusSection((await get("/autonomy/status")).data)),
      asyncSection(t("autonomy.suggestions"), async () => {
        const {data} = await get("/autonomy/suggestions", {limit: 50});
        return data.length ? suggestionsSection(data) : h("p", {class: "muted"}, t("autonomy.empty"));
      }),
      asyncSection(t("autonomy.runs"), async () => {
        const {data} = await get("/autonomy/runs", {limit: 50});
        return data.length ? runsSection(data) : h("p", {class: "muted"}, t("autonomy.empty"));
      }),
      asyncSection(t("autonomy.anomalies"), async () => {
        const {data} = await get("/autonomy/anomalies", {limit: 50});
        return data.length ? anomaliesSection(data) : h("p", {class: "muted"}, t("autonomy.empty"));
      }));
  }

  load();
  return h("div", {}, h("h1", {}, t("autonomy.title")), h("p", {class: "muted"}, t("autonomy.description")), status, formArea, area);
}
