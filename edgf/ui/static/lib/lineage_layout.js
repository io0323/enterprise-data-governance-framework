// 自前の階層レイアウト。level (起点からの距離) を列にし、列の中は隣接ノードの位置の平均で並べて交差を減らす。
export const NODE_WIDTH = 210;
export const NODE_HEIGHT = 46;
const GAP_X = 70;
const GAP_Y = 16;
const MARGIN = 14;

export function layout(nodes, edges) {
  const columns = new Map();
  for (const node of nodes) {
    if (!columns.has(node.level)) columns.set(node.level, []);
    columns.get(node.level).push(node);
  }
  const levels = [...columns.keys()].sort((a, b) => a - b);
  const order = new Map();
  const neighbors = new Map(nodes.map((n) => [n.qualified_name, []]));
  for (const edge of edges) {
    neighbors.get(edge.from)?.push(edge.to);
    neighbors.get(edge.to)?.push(edge.from);
  }
  for (const level of levels) {
    const column = columns.get(level);
    const score = (node) => {
      const placed = neighbors.get(node.qualified_name).filter((n) => order.has(n)).map((n) => order.get(n));
      return placed.length ? placed.reduce((a, b) => a + b, 0) / placed.length : Number.MAX_SAFE_INTEGER;
    };
    column.sort((a, b) => score(a) - score(b) || a.qualified_name.localeCompare(b.qualified_name));
    column.forEach((node, index) => order.set(node.qualified_name, index));
  }
  const tallest = Math.max(...levels.map((l) => columns.get(l).length), 1);
  const height = MARGIN * 2 + tallest * NODE_HEIGHT + (tallest - 1) * GAP_Y;
  const positions = new Map();
  levels.forEach((level, x) => {
    const column = columns.get(level);
    const used = column.length * NODE_HEIGHT + (column.length - 1) * GAP_Y;
    const top = (height - used) / 2;
    column.forEach((node, y) => positions.set(node.qualified_name, {
      x: MARGIN + x * (NODE_WIDTH + GAP_X), y: top + y * (NODE_HEIGHT + GAP_Y),
    }));
  });
  return {positions, width: MARGIN * 2 + levels.length * NODE_WIDTH + (levels.length - 1) * GAP_X, height};
}

/** ノードの右端から次のノードの左端へのベジェ曲線。同じ列や逆向き (循環) でも描ける。 */
export function edgePath(from, to) {
  const x1 = from.x + NODE_WIDTH;
  const y1 = from.y + NODE_HEIGHT / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_HEIGHT / 2;
  const bend = Math.max(Math.abs(x2 - x1) / 2, 30);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
}
