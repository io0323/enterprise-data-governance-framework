// §6.2 Business Glossary: 一覧と詳細。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {ApiError, get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {PAGE_SIZE, asyncSection, definitions, loading, pager, problemView, section, table} from "../lib/components.js";

function listView(params) {
  const offset = Number(params.offset || 0);
  return h("div", {},
    h("h1", {}, t("glossary.title")),
    section(t("glossary.search"), h("form", {class: "filters", onSubmit: (event) => {
      event.preventDefault();
      const q = new FormData(event.target).get("q");
      navigate(["glossary"], q ? {q} : {});
    }},
    h("label", {}, t("glossary.search"), h("input", {type: "search", name: "q", value: params.q || ""})),
    h("button", {type: "submit", class: "primary"}, t("catalog.search")))),
    asyncSection(t("catalog.results"), async () => {
      const result = await get("/glossary/terms", {q: params.q, limit: PAGE_SIZE, offset});
      return h("div", {}, table(t("glossary.title"), [
        {title: t("glossary.name"), render: (term) => h("a", {href: hrefFor(["glossary", term.id])}, term.name)},
        {title: t("glossary.definition"), render: (term) => term.definition},
        {title: t("glossary.steward"), render: (term) => term.steward || "-"},
      ], result.data), pager(result, (next) => navigate(["glossary"], {...params, offset: next})));
    }));
}

function detailView(id) {
  const root = h("div", {}, h("h1", {}, t("glossary.title")), loading());
  get(`/glossary/terms/${encodeURIComponent(id)}`).then(async ({data: term}) => {
    const assets = await Promise.all(term.asset_ids.map((assetId) => get(`/assets/${assetId}`).then((r) => r.data)));
    root.replaceChildren(
      h("p", {}, h("a", {href: "#/glossary"}, t("glossary.back"))),
      h("h1", {}, term.name),
      section(t("glossary.definition"), definitions([
        [t("glossary.definition"), term.definition], [t("glossary.steward"), term.steward || "-"], [t("glossary.synonyms"), (term.synonyms || []).join(", ") || "-"],
      ])),
      section(t("glossary.assets"), h("p", {class: "muted"}, t("glossary.hidden")),
        assets.length ? h("ul", {}, assets.map((a) => h("li", {}, h("a", {href: hrefFor(["assets", a.id])}, a.qualified_name)))) : h("p", {class: "muted"}, t("common.empty"))));
  }).catch((error) => {
    if (!(error instanceof ApiError && error.status === 401)) root.replaceChildren(h("h1", {}, t("glossary.title")), problemView(error));
  });
  return root;
}

export function glossaryPage(segments, params) {
  return segments[0] ? detailView(segments[0]) : listView(params);
}
