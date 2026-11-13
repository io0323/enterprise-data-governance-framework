// 判断のフォーム (Phase 17)。承認箱、棚卸と証明、Issue で共通。
// operation はサーバ (承認箱のアダプタなど) が返す {id, method, path, body, fields, step_up}。送信は actions.js だけが行う。
import {h} from "./dom.js";
import {t} from "./i18n.js";
import {ApiError} from "./api.js";
import * as actions from "./actions.js";

function input(field, id) {
  const common = {id, name: field.name, required: field.required || null, "aria-required": field.required ? "true" : null};
  if (field.kind === "select") {
    return h("select", common, h("option", {value: ""}, t("form.unchanged")),
      field.options.map((option) => h("option", {value: option}, t(`form.option.${option}`))));
  }
  if (field.kind === "datetime") return h("input", {...common, type: "datetime-local"});
  return h("textarea", {...common, rows: "3", maxlength: field.max_length ? String(field.max_length) : null});
}

function valueOf(field, element) {
  const raw = element.value.trim();
  if (!raw) return null;
  return field.kind === "datetime" ? new Date(raw).toISOString() : raw;
}

/** 判断の 1 件のフォーム。確定で送信し、成功したら onDone を呼ぶ。Esc か取消で onCancel。 */
export function decisionForm({title, operation, onDone, onCancel}) {
  const error = h("p", {class: "form-error", role: "alert"});
  const inputs = operation.fields.map((field) => [field, input(field, `field-${field.name}`)]);
  const submit = h("button", {type: "submit", class: operation.id === "reject" ? "danger" : "primary", id: "decision-submit"}, t("form.confirm"));
  const form = h("form", {class: "decision-form panel", id: "decision-form", "aria-labelledby": "decision-title", novalidate: true,
    onKeydown: (event) => {
      if (event.key === "Escape") onCancel();
    },
    onSubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      const body = {...operation.body};
      for (const [field, element] of inputs) {
        const value = valueOf(field, element);
        if (field.required && value === null) {
          error.textContent = t(operation.id === "reject" ? "form.reason_required" : "form.required", {field: t(`form.field.${field.name}`)});
          element.focus();
          return;
        }
        if (value !== null) body[field.name] = value;
      }
      submit.disabled = true;
      try {
        await actions.call(operation.method, operation.path, body);
        onDone(operation);
      } catch (failure) {
        submit.disabled = false;
        if (failure instanceof ApiError && failure.status === 401) return; // 再ログインへ移動している
        error.textContent = failure instanceof ApiError ? (failure.problem?.detail || t("error.generic")) : t("error.generic");
      }
    }},
    h("h2", {id: "decision-title"}, title, " - ", t(`form.op.${operation.id}`)),
    operation.step_up ? h("p", {class: "muted"}, t("form.step_up_note")) : null,
    inputs.map(([field, element]) => h("div", {class: "field"},
      h("label", {for: element.id}, t(`form.field.${field.name}`), field.required ? ` (${t("form.required_mark")})` : ""), element)),
    error,
    h("div", {class: "form-actions"}, submit, h("button", {type: "button", id: "decision-cancel", onClick: onCancel}, t("form.cancel"))));
  return form;
}

/** 操作のボタンの並び。押すと onSelect(operation)。itemId は案件の識別 (data-item) */
export function operationButtons(operations, onSelect, label = "", itemId = null) {
  return h("div", {class: "op-buttons", role: "group", "aria-label": label || null, "data-item": itemId},
    operations.map((operation) => h("button", {type: "button", class: operation.id === "reject" ? "danger" : null, "data-op": operation.id,
      onClick: () => onSelect(operation)}, t(`form.op.${operation.id}`))));
}
