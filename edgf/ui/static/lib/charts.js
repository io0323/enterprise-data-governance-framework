// 自前の SVG の折れ線グラフ。値は表でも示すので、グラフは視覚的な補助。
import {h, s} from "./dom.js";
import {t} from "./i18n.js";

const WIDTH = 720;
const HEIGHT = 220;
const PAD = {left: 46, right: 12, top: 12, bottom: 28};

/** series: [{label, values: [number|null]}]。labels は横軸の文字列。percent=true で 0〜100% 表示。 */
export function lineChart(title, labels, series, {percent = true} = {}) {
  const all = series.flatMap((x) => x.values).filter((v) => v !== null && v !== undefined);
  const svg = s("svg", {class: "chart", viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: "img", "aria-label": all.length ? title : `${title}: ${t("chart.no_data")}`});
  svg.append(s("title", {}, title));
  const low = percent ? 0 : Math.min(0, ...all);
  const high = percent ? 1 : Math.max(1, ...all);
  const x = (i) => PAD.left + (labels.length > 1 ? (i * (WIDTH - PAD.left - PAD.right)) / (labels.length - 1) : 0);
  const y = (v) => HEIGHT - PAD.bottom - ((v - low) / (high - low || 1)) * (HEIGHT - PAD.top - PAD.bottom);
  for (let step = 0; step <= 4; step += 1) {
    const value = low + ((high - low) * step) / 4;
    svg.append(s("line", {class: "axis", x1: PAD.left, x2: WIDTH - PAD.right, y1: y(value), y2: y(value)}));
    svg.append(s("text", {x: PAD.left - 6, y: y(value) + 4, "text-anchor": "end"}, percent ? `${Math.round(value * 100)}%` : `${Math.round(value)}`));
  }
  const edge = [0, labels.length - 1];
  for (const i of new Set(edge)) {
    if (labels[i] !== undefined) svg.append(s("text", {x: x(i), y: HEIGHT - 8, "text-anchor": i === 0 ? "start" : "end"}, labels[i]));
  }
  series.forEach((line, index) => {
    const points = line.values.map((v, i) => (v === null || v === undefined ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean);
    if (points.length > 1) svg.append(s("polyline", {class: `series series-${index}`, points: points.join(" ")}));
    line.values.forEach((v, i) => {
      if (v !== null && v !== undefined) svg.append(s("circle", {class: `point point-${index}`, cx: x(i), cy: y(v), r: 3}, s("title", {}, `${line.label} ${labels[i]}: ${percent ? `${(v * 100).toFixed(1)}%` : v}`)));
    });
  });
  return svg;
}

/** 凡例。色だけに頼らないよう、線種の見本も付ける (WCAG 1.4.1)。 */
export function chartLegend(labels) {
  return h("ul", {class: "chart-legend"}, labels.map((label, index) => h("li", {},
    s("svg", {width: 28, height: 10, "aria-hidden": "true"}, s("line", {class: `series series-${index}`, x1: 0, x2: 28, y1: 5, y2: 5})), label)));
}
