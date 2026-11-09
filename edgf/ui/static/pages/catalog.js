// §7 Search: 検索と結果。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {PAGE_SIZE, asyncSection, badge, certificationBadge, pager, section, sensitivityLabel, table} from "../lib/components.js";

const DATA_CLASSES = ["master", "reference", "transactional", "analytical", "operational", "ai", "iot", "metadata"];
const SENSITIVITIES = ["public", "internal", "confidential", "restricted"];
const CERTIFICATIONS = ["draft", "certified", "deprecated"];
const LIFECYCLES = ["draft", "active", "deprecated", "archived", "deleted"];

function select(name, label, values, current) {
  return h("label", {}, label,
    h("select", {name}, h("option", {value: ""}, t("catalog.any")),
      values.map((v) => h("option", {value: v, selected: v === current}, v))));
}

function filters(params) {
  const domainSelect = h("select", {name: "domain"}, h("option", {value: ""}, t("catalog.any")),
    params.domain ? h("option", {value: params.domain, selected: true}, params.domain) : null);
  get("/domains", {limit: 1000}).then(({data}) => {
    for (const domain of data) {
      if (domain.name !== params.domain) domainSelect.append(h("option", {value: domain.name}, domain.name));
    }
  }).catch(() => {});
  return h("form", {class: "filters", "aria-label": t("catalog.filters"), onSubmit: (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const next = {};
    for (const [key, value] of form.entries()) {
      if (value) next[key] = value;
    }
    navigate(["catalog"], next);
  }},
  h("label", {}, t("catalog.keyword"), h("input", {type: "search", name: "q", value: params.q || "", id: "catalog-q"})),
  h("label", {}, t("catalog.domain"), domainSelect),
  select("data_class", t("catalog.data_class"), DATA_CLASSES, params.data_class),
  select("sensitivity", t("catalog.sensitivity"), SENSITIVITIES, params.sensitivity),
  select("certification", t("catalog.certification"), CERTIFICATIONS, params.certification),
  select("lifecycle_state", t("catalog.lifecycle"), LIFECYCLES, params.lifecycle_state),
  h("label", {class: "inline"}, h("input", {type: "checkbox", name: "is_cde", value: "true", checked: params.is_cde === "true"}), t("catalog.is_cde")),
  h("label", {class: "inline"}, h("input", {type: "checkbox", name: "orphan", value: "true", checked: params.orphan === "true"}), t("catalog.orphan")),
  h("button", {type: "submit", class: "primary"}, t("catalog.search")),
  h("a", {href: "#/catalog"}, t("catalog.clear")));
}

const COLUMNS = () => [
  {title: t("catalog.name"), render: (a) => h("a", {href: hrefFor(["assets", a.id])}, a.qualified_name)},
  {title: t("catalog.domain"), render: (a) => a.domain || "-"},
  {title: t("catalog.data_class"), render: (a) => a.data_class},
  {title: t("catalog.sensitivity"), render: (a) => sensitivityLabel(a.effective_sensitivity || a.sensitivity)},
  {title: t("catalog.certification"), render: (a) => certificationBadge(a.certification)},
  {title: t("catalog.lifecycle"), render: (a) => a.lifecycle_state},
  {title: t("catalog.owner"), render: (a) => a.owner || badge(t("catalog.none_owner"), "bad")},
];

export function catalogPage(_segments, params) {
  const offset = Number(params.offset || 0);
  const query = {...params, limit: PAGE_SIZE, offset};
  return h("div", {},
    h("h1", {}, t("catalog.title")),
    section(t("catalog.filters"), filters(params)),
    asyncSection(t("catalog.results"), async () => {
      const result = await get("/assets", query);
      return h("div", {id: "catalog-results"}, table(t("catalog.results"), COLUMNS(), result.data),
        pager(result, (next) => navigate(["catalog"], {...params, offset: next})));
    }));
}
