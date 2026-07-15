/* Decorative digital rain for the home view's empty right-hand pane — pure
   ambiance, no data, aria-hidden. Deliberately slow: a stepped interval
   rather than a 60fps loop, dim trails, sparse glyph set drawn from the
   app's own icon vocabulary rather than generic Matrix katakana. Runs only
   while #home-view is visible; state.js's showView() calls stopRain via
   hooks.stopHomeRain the moment the user navigates elsewhere. */
"use strict";

import { $ } from "./lib.js";
import { hooks } from "./state.js";

const GLYPHS = "01▣◈▦✦│┃╎01010101ABCDEF".split("");
const COL_W = 18;              // px between columns
const ROW_H = 16;              // px a glyph advances per step
const STEP_MS = 110;           // ms between steps — the "slow" in slow rain
const BG_FADE = "rgba(10, 14, 23, 0.14)";  // low-alpha repaint of --bg = long dim trail
const HEAD_COLOR = "#00e5ff";
const TRAIL_COLOR = "rgba(0, 229, 255, 0.28)";

let pane = null, canvas = null, ctx = null, cols = [], timer = null, ro = null;

function buildColumns(width, height) {
  const count = Math.max(1, Math.floor(width / COL_W));
  cols = Array.from({ length: count }, () => ({
    y: -Math.floor(Math.random() * (height / ROW_H || 1)),
    speed: Math.random() < 0.5 ? 1 : 2,       // most columns crawl; a few drift a touch faster
    next: Math.floor(Math.random() * 6),      // stagger so columns don't step in lockstep
  }));
}

function resize() {
  if (!pane || !canvas || !ctx) return;
  const rect = pane.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = "13px ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, Consolas, monospace";
  ctx.textBaseline = "top";
  ctx.fillStyle = "#0a0e17";
  ctx.fillRect(0, 0, rect.width, rect.height);
  buildColumns(rect.width, rect.height);
}

function glyph() { return GLYPHS[(Math.random() * GLYPHS.length) | 0]; }

function tick() {
  if (!ctx || !canvas) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.fillStyle = BG_FADE;
  ctx.fillRect(0, 0, w, h);
  cols.forEach((c, i) => {
    c.next -= 1;
    if (c.next > 0) return;
    c.next = c.speed;
    const x = i * COL_W + 3;
    ctx.fillStyle = TRAIL_COLOR;
    ctx.fillText(glyph(), x, c.y - ROW_H);
    ctx.fillStyle = HEAD_COLOR;
    ctx.fillText(glyph(), x, c.y);
    c.y += ROW_H;
    if (c.y > h + ROW_H * 2) c.y = -Math.floor(Math.random() * (h / ROW_H || 1)) * ROW_H;
  });
}

export function startRain() {
  pane = $("#home-rain");
  canvas = $("#home-rain-canvas");
  if (!pane || !canvas || timer) return;   // no pane on this layout, or already running
  if (pane.offsetWidth === 0) return;      // pane is display:none at this viewport width
  ctx = canvas.getContext("2d");
  resize();
  ro = new ResizeObserver(resize);
  ro.observe(pane);
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    tick();   // one static frame of scattered glyphs — texture without motion
    return;
  }
  timer = setInterval(tick, STEP_MS);
}

export function stopRain() {
  if (timer) { clearInterval(timer); timer = null; }
  if (ro) { ro.disconnect(); ro = null; }
  ctx = null; canvas = null; pane = null; cols = [];
}
hooks.stopHomeRain = stopRain;
