/* Instant cross-filter: client-side re-aggregation (specs/016-instant-cross-filter/).

   Perspective is used here as one thing only — a headless columnar aggregation
   engine. Its `Table`/`View` API takes the Arrow extract a tile fetched once
   and answers every subsequent cross-filter, grain toggle, or focus change
   from it, with no network call. Nothing Perspective renders is ever used:
   slice() hands back the same `{columns, rows}` shape /api/query returns, so
   every chart renderer in ./charts/ works unchanged (FR-004/FR-005).

   The module (and its ~2.5MB of wasm) is loaded by a dynamic import() that
   only ever runs for a dashboard with `instant: true` — see boot(). Nothing
   else in the app pays for it, and it is served from app/static/vendor/,
   never a CDN (FR-011). */
"use strict";

const VENDOR = "/static/vendor/perspective";

// Perspective's aggregate names for the roll-up aggregations the extract
// endpoint plans in neutral terms (app/extract.py).
const AGGREGATES = { sum: "sum", min: "min", max: "max", mean: "avg" };

/** A change the cached extract genuinely cannot answer — a finer grain than
 *  it was fetched at, or a filter on a dimension it doesn't carry. The caller
 *  answers this one interaction with a real query (FR-007); it is not a
 *  failure, and the tile stays instant. */
export class NeedsRefetch extends Error {}

let booting = null;     // the one in-flight/settled boot for this page

/** Load Perspective and start its worker. Idempotent, and shared by every
 *  tile on the page: one wasm compile, one worker, N tables. Rejects if the
 *  module or the wasm can't be loaded at all, which is the caller's cue to
 *  run the whole dashboard live for the session. */
export function boot() {
  if (!booting) {
    booting = (async () => {
      const psp = await import(`${VENDOR}/cdn/perspective.js`);
      // the module registers its own server wasm relative to its URL; the
      // client wasm has no such self-registration and is handed over here
      psp.init_client(fetch(`${VENDOR}/wasm/perspective-js.wasm`));
      return psp.default.worker();
    })().catch((err) => { booting = null; throw err; });
  }
  return booting;
}

/** Build a tile's extract from one extract-endpoint response. `buffer` is the
 *  raw Arrow IPC body, `meta` the decoded X-Extract-Meta payload. */
export async function makeExtract(buffer, meta) {
  const client = await boot();
  const table = await client.table(buffer);
  return new Extract(table, meta);
}

class Extract {
  constructor(table, meta) {
    this.table = table;
    this.meta = meta;
    this.dims = new Map(meta.dimensions.map((d) => [d.name, d]));
    // every component column the measures need, deduped — two measures
    // sharing an aggregate share one column — plus the geo coordinate
    // columns, which ride along as ordinary averaged passthroughs
    this.aggregates = {};
    for (const m of meta.measures) for (const c of m.components) this.aggregates[c.col] = AGGREGATES[c.agg];
    for (const p of meta.passthrough) this.aggregates[p.col] = AGGREGATES[p.agg];
    this.columns = Object.keys(this.aggregates);
  }

  /** True if this extract can answer a filter on `field` — i.e. it carries
   *  that column. A field the tile's *model* doesn't have never reaches here
   *  (that stays today's silent no-op); a field the model has but the extract
   *  doesn't means this interaction needs a real query. */
  canFilter(field) { return this.dims.has(field); }

  /** The extract column holding `name` at `grain`: the dimension itself when
   *  the grain is unchanged, a precomputed coarser bucket when the session
   *  grain override asks for one, and nothing at all when it asks for
   *  something finer than was fetched. */
  column(name, grain) {
    const dim = this.dims.get(name);
    if (!dim) throw new NeedsRefetch(`extract has no dimension '${name}'`);
    if (!grain || grain === dim.grain) return dim.name;
    const coarser = (dim.coarser || {})[grain];
    if (!coarser) {
      throw new NeedsRefetch(
        `grain '${grain}' is finer than the extract's '${dim.grain || "none"}' for '${name}'`);
    }
    return coarser;
  }

  /** Re-aggregate the extract down to one tile's own shape.
   *
   *  `dims`    the tile's displayed dimensions, [{name, grain}]
   *  `filters` cross-filter equality terms, [{field, value}]
   *  `sort`    the tile's saved sort, {by, desc} or null
   *  `limit`   the tile's saved row limit
   *
   *  Returns the same object shape /api/query does, so the caller can drop it
   *  straight into a chart ctx.
   */
  async slice({ dims = [], filters = [], sort = null, limit = 1000 } = {}) {
    const started = performance.now();
    const groupBy = dims.map((d) => this.column(d.name, d.grain));
    for (const f of filters) {
      if (!this.canFilter(f.field)) throw new NeedsRefetch(`extract has no dimension '${f.field}'`);
    }
    const filter = filters.map((f) => [f.field, "==", f.value]);

    // Perspective returns a grand-total row (an empty row path) ahead of the
    // groups, which is exactly what a dimensionless stat tile wants — and the
    // only way to get one, since group_by: [] leaves the rows ungrouped. So
    // there is always something to group by, and the depth filter below keeps
    // whichever rows this tile actually asked for.
    const grouped = groupBy.length ? groupBy : [this.dims.keys().next().value || this.columns[0]];
    const view = await this.table.view({
      group_by: grouped, filter, aggregates: this.aggregates, columns: this.columns,
    });
    let cells;
    try {
      cells = await view.to_columns();
    } finally {
      await view.delete();
    }

    const paths = cells.__ROW_PATH__ || [];
    const rows = [];
    for (let i = 0; i < paths.length; i++) {
      if (paths[i].length !== groupBy.length) continue;   // skip totals/subtotals
      const row = {};
      dims.forEach((d, k) => { row[d.name] = paths[i][k]; });
      for (const m of this.meta.measures) {
        row[m.name] = evaluate(m.formula, m.components.map((c) => cells[c.col][i]));
      }
      for (const p of this.meta.passthrough) row[p.col] = cells[p.col][i];
      rows.push(row);
    }

    order(rows, dims, this.meta, sort);
    const cut = rows.slice(0, limit || rows.length);
    return {
      columns: this.meta.columns,
      rows: cut,
      row_count: cut.length,
      elapsed_ms: Math.round((performance.now() - started) * 10) / 10,
      instant: true,
    };
  }

  destroy() {
    // views are deleted as they're read (a table refuses to close while one
    // is open), so the table is all that's left to hand back
    const table = this.table;
    this.table = null;
    if (table) table.delete().catch(() => {});
  }
}

/** Recompute a measure from its rolled-up components. Nulls propagate, so a
 *  bucket nothing landed in stays a gap rather than becoming a zero. */
function evaluate(node, refs) {
  if ("const" in node) return node.const;
  if ("ref" in node) return refs[node.ref];
  const l = evaluate(node.l, refs);
  const r = evaluate(node.r, refs);
  if (l === null || l === undefined || r === null || r === undefined) return null;
  if (node.op === "+") return l + r;
  if (node.op === "-") return l - r;
  if (node.op === "*") return l * r;
  return l / r;
}

/** The engine's own ordering rule, applied to the locally re-aggregated rows:
 *  the tile's saved sort when it names something the result actually has,
 *  else time ascending if there's a time dimension, else the first measure
 *  descending (engine._run_single). Nulls sort last either way. */
function order(rows, dims, meta, sort) {
  const measures = meta.measures.map((m) => m.name);
  const names = new Set([...dims.map((d) => d.name), ...measures]);
  let by = sort && sort.by && names.has(sort.by) ? sort.by : null;
  let desc = by ? !!sort.desc : true;
  if (!by) {
    if (!dims.length) return;
    const time = dims.find((d) => (meta.dimensions.find((x) => x.name === d.name) || {}).type === "time");
    by = time ? time.name : measures[0];
    desc = !time;
  }
  rows.sort((a, b) => {
    const x = a[by], y = b[by];
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * (desc ? -1 : 1);
  });
}
