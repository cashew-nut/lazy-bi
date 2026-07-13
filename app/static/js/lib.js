/* DOM, fetch, and formatting primitives — no app state. */
"use strict";

export const $ = (sel) => document.querySelector(sel);

export const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
};

export const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    // X-Requested-With is the CSRF gate: the server refuses cookie-authed
    // mutations without it, and cross-site pages cannot set it (spec 011)
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch",
               ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    // session expired or revoked: reopen the login overlay without a page
    // navigation so in-memory drafts survive re-authentication
    window.dispatchEvent(new Event("auth-required"));
  }
  if (res.status === 204) return null;
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// ── number/date formatting ───────────────────────────────────

export function abbrev(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "K";
  if (a >= 100 || Number.isInteger(v)) return Math.round(v).toLocaleString();
  return v.toFixed(2);
}

export function fmtMeasure(v, format, compact = true) {
  if (v === null || v === undefined) return "∅";
  if (format === "percent") return (v * 100).toFixed(1) + "%";
  if (format === "currency") return "$" + (compact ? abbrev(v) : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));
  return compact ? abbrev(v) : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtDateLabel(iso, grain) {
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return String(iso);
  const [, y, mo, d] = m;
  if (grain === "1y") return y;
  if (grain === "1q") return "Q" + (Math.floor((+mo - 1) / 3) + 1) + " '" + y.slice(2);
  if (grain === "1mo") return MONTHS[+mo - 1] + " '" + y.slice(2);
  return d + " " + MONTHS[+mo - 1] + " '" + y.slice(2);
}

export function fmtBytes(b) {
  if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
  if (b >= 1e3) return (b / 1e3).toFixed(1) + " KB";
  return b + " B";
}
