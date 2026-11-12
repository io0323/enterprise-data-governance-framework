// §9 Data Lineage: 上流・下流のグラフと影響分析。
import {h, s} from "../lib/dom.js";
import {t} from "../lib/i18n.js";
import {get} from "../lib/api.js";
import {hrefFor, navigate} from "../lib/router.js";
import {NODE_HEIGHT, NODE_WIDTH, edgePath, layout} from "../lib/lineage_layout.js";
import {asyncSection, badge, definitions, section, table} from "../lib/components.js";

const DEPTHS = [1, 2, 3, 5, 10];
const MAX_LABEL = 28;

function nodeTags(node) {
  if (node.redacted) return [t("lineage.redacted")];
  if (!node.registered) return [t("lineage.unregistered")];
  return [node.is_cde ? t("lineage.cde") : null, node.certified ? t("lineage.certified") : null].filter(Boolean);
}

function nodeClass(node, root) {
  return ["node", node.qualified_name === root ? "node-root" : "", node.redacted ? "node-redacted" : "", !node.registered ? "node-unregistered" : "",
    node.is_cde ? "node-cde" : "", node.certified ? "node-certified" : ""].filter(Boolean).join(" ");
}

function nodeShape(node, position, root) {
  const name = node.qualified_name.length > MAX_LABEL ? `${node.qualified_name.slice(0, MAX_LABEL - 1)}…` : node.qualified_name;
  const group = s("g", {class: nodeClass(node, root), transform: `translate(${position.x} ${position.y})`},
    s("title", {}, [node.qualified_name, ...nodeTags(node)].join(" / ")),
    s("rect", {width: NODE_WIDTH, height: NODE_HEIGHT}),
    s("text", {x: 8, y: 19}, name),
    s("text", {x: 8, y: 36, class: "tag"}, nodeTags(node).join(" · ")));
  if (node.asset_id && node.qualified_name !== root) {
    return s("a", {href: hrefFor(["lineage", node.qualified_name]), "aria-label": `${node.qualified_name} ${nodeTags(node).join(" ")}`}, group);
  }
  return group;
}

function graphView(graph) {
  const {positions, width, height} = layout(graph.nodes, graph.edges);
  const svg = s("svg", {class: "lineage", width, height, viewBox: `0 0 ${width} ${height}`, role: "group",
    "aria-label": t("lineage.graph_label", {name: graph.asset, nodes: graph.nodes.length, edges: graph.edges.length})},
  s("defs", {}, s("marker", {id: "arrow", markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto"}, s("path", {d: "M 0 0 L 8 4 L 0 8 z", class: "arrow"}))));
  for (const edge of graph.edges) {
    const from = positions.get(edge.from);
    const to = positions.get(edge.to);
    if (from && to) {
      svg.append(s("path", {class: "edge", d: edgePath(from, to), "marker-end": "url(#arrow)"}, s("title", {}, `${edge.from} → ${edge.to}${edge.job ? ` (${edge.job})` : ""}`)));
    }
  }
  for (const node of graph.nodes) {
    svg.append(nodeShape(node, positions.get(node.qualified_name), graph.asset));
  }
  return svg;
}

function directionOf(level) {
  if (level === 0) return t("lineage.root");
  return level < 0 ? t("lineage.upstream") : t("lineage.downstream");
}

function nodeTable(graph) {
  return table(t("lineage.nodes"), [
    {title: t("lineage.node"), render: (n) => (n.asset_id ? h("a", {href: hrefFor(["assets", n.asset_id])}, n.qualified_name) : n.qualified_name)},
    {title: t("lineage.direction"), render: (n) => directionOf(n.level)},
    {title: t("lineage.distance"), render: (n) => Math.abs(n.level)},
    {title: t("lineage.attributes"), render: (n) => nodeTags(n).join(", ") || "-"},
  ], graph.nodes);
}

const RISK_KIND = {high: "bad", medium: "warn", none: "ok"};

function impactView(impact) {
  return h("div", {},
    definitions([
      [t("lineage.risk"), badge(t(`lineage.risk.${impact.risk}`), RISK_KIND[impact.risk] || "neutral")],
      [t("lineage.impacted_count"), impact.impacted_count],
      [t("lineage.owners"), impact.owners_to_notify.join(", ") || "-"],
      [t("lineage.unregistered_downstream"), impact.unregistered_downstream.join(", ") || "-"],
    ]),
    table(t("lineage.impacted"), [
      {title: t("lineage.node"), render: (i) => (i.asset_id ? h("a", {href: hrefFor(["assets", i.asset_id])}, i.qualified_name) : i.qualified_name)},
      {title: t("lineage.distance"), render: (i) => i.depth},
      {title: t("lineage.attributes"), render: (i) => (i.redacted ? t("lineage.redacted") : [i.registered ? null : t("lineage.unregistered"), i.is_cde ? t("lineage.cde") : null, i.certified ? t("lineage.certified") : null].filter(Boolean).join(", ") || "-")},
      {title: t("lineage.owner"), render: (i) => i.owner || "-"},
    ], impact.impacted));
}

function columnGraph(root, upstream, downstream) {
  // カラム単位のグラフ: ノードは「資産・カラム」。閲覧できない資産はラベルだけで、カラムは伏せる
  const key = (name, col) => `${name} · ${col ?? "*"}`;
  const rootKey = key(root.asset, root.column);
  const nodes = new Map([[rootKey, {qualified_name: rootKey, level: 0, registered: true}]]);
  const edges = [];
  for (const [result, sign] of [[upstream, -1], [downstream, 1]]) {
    for (const e of result.edges) {
      const from = key(e.from, e.from_column);
      const to = key(e.to, e.to_column);
      const far = sign < 0 ? from : to;
      const redacted = sign < 0 ? e.from_redacted : e.to_redacted;
      if (!nodes.has(far)) nodes.set(far, {qualified_name: far, level: sign * e.depth, registered: true, redacted});
      edges.push({from, to, job: e.job ? `${e.job} (${e.type})` : e.type});
    }
  }
  return {asset: rootKey, nodes: [...nodes.values()], edges};
}

function columnView(name, column, depth) {
  const path = encodeURIComponent(name);
  const col = encodeURIComponent(column);
  return asyncSection(t("lineage.column_graph", {column}), async () => {
    const [up, down, impact] = await Promise.all([get(`/lineage/${path}/columns/${col}/upstream`, {depth}), get(`/lineage/${path}/columns/${col}/downstream`, {depth}),
      get(`/lineage/${path}/columns/${col}/impact`, {depth})]);
    const graph = columnGraph(up.data, up.data, down.data);
    return h("div", {},
      h("div", {class: "lineage-scroll", tabindex: "0", role: "region", "aria-label": t("lineage.column_graph", {column})}, graphView(graph)),
      h("h3", {}, t("lineage.column_impact")),
      table(t("lineage.column_impact"), [
        {title: t("lineage.node"), render: (i) => (i.redacted ? `${i.qualified_name} (${t("lineage.redacted")})` : `${i.qualified_name} · ${i.column ?? "*"}`)},
        {title: t("lineage.distance"), render: (i) => i.depth},
        {title: t("lineage.path"), render: (i) => i.path},
        {title: t("lineage.attributes"), render: (i) => (i.cde_column ? t("lineage.cde") : "-")},
        {title: t("lineage.owner"), render: (i) => i.owner || "-"},
      ], impact.data.impacted));
  });
}

function form(name, depth, column) {
  return h("form", {class: "filters", onSubmit: (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const target = String(data.get("name") || "").trim();
    if (target) navigate(["lineage", target], {depth: data.get("depth"), column: String(data.get("column") || "").trim()});
  }},
  h("label", {}, t("lineage.asset"), h("input", {type: "search", name: "name", value: name || "", required: true})),
  h("label", {}, t("lineage.depth"), h("select", {name: "depth"}, DEPTHS.map((d) => h("option", {value: d, selected: d === depth}, d)))),
  h("label", {}, t("lineage.column"), h("input", {type: "search", name: "column", value: column || ""})),
  h("button", {type: "submit", class: "primary"}, t("lineage.show")));
}

export function lineagePage(segments, params) {
  const name = segments[0];
  const depth = Number(params.depth || 3);
  if (!name) {
    return h("div", {}, h("h1", {}, t("lineage.title")), section(t("lineage.asset"), h("p", {class: "muted"}, t("lineage.hint")), form("", depth, "")));
  }
  const path = encodeURIComponent(name);
  return h("div", {},
    h("h1", {}, `${t("lineage.title")}: ${name}`),
    section(t("lineage.asset"), form(name, depth, params.column)),
    params.column ? columnView(name, params.column, depth) : null,
    asyncSection(t("lineage.graph"), async () => {
      const {data} = await get(`/lineage/${path}/graph`, {depth});
      return h("div", {},
        h("p", {class: "legend"}, badge(t("lineage.root"), "neutral"), badge(t("lineage.cde"), "warn"), badge(t("lineage.certified"), "ok"), badge(t("lineage.unregistered"), "neutral"), badge(t("lineage.redacted"), "neutral")),
        h("div", {class: "lineage-scroll", tabindex: "0", role: "region", "aria-label": t("lineage.graph")}, graphView(data)),
        h("h3", {}, t("lineage.nodes")), nodeTable(data));
    }),
    asyncSection(t("lineage.impact"), async () => impactView((await get(`/lineage/${path}/impact`, {depth})).data)));
}
