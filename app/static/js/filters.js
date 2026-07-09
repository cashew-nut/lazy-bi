/* Filter vocabulary shared by the builder, dashboard views, and focus mode. */
"use strict";

export const FILTER_OPS = [
  ["eq", "="], ["ne", "≠"], ["gt", ">"], ["gte", "≥"], ["lt", "<"], ["lte", "≤"],
  ["in", "in"], ["not_in", "not in"], ["contains", "contains"],
];

export const filterReady = (f) =>
  f.op === "in" || f.op === "not_in" ? f.values.length : f.value !== "" && f.value != null;

export const toApiFilter = (f) =>
  f.op === "in" || f.op === "not_in"
    ? { field: f.field, op: f.op, values: f.values }
    : { field: f.field, op: f.op, value: f.value };
