/* Theme catalog + switching engine (spec 013).
   Owns the 4 pre-packed themes and is the single place a theme's values are
   defined: CSS custom-property overrides live in style.css keyed by the same
   [data-theme] ids as THEMES below; here we own the parts CSS can't reach —
   the categorical chart palette (mutated in place on charts/common.js's
   PALETTE/OTHER_COLOR, per research.md §2) and the decorative-effects /
   validate_palette.js "mode" metadata. */
"use strict";

import { PALETTE, setOtherColor } from "./charts/common.js";
import { api } from "./lib.js";

// Each theme's chartPalette + chartOtherColor is validated (FR-008) via
// app/static/validate_palette.js against that theme's own --bg (style.css).
// Slate and Contrast reuse the cyberpunk palette because it independently
// passes against both of their (also-dark) surfaces — there's no requirement
// that every theme's data colors differ, only that each one is proven for
// its own surface. Daylight needs its own: a dark-surface palette fails
// WCAG contrast against a light background.
const CYBERPUNK_PALETTE = ["#0099ad", "#a68f00", "#d633b8", "#eb6234", "#3d7dd6", "#1fae57", "#8b63f2", "#d64f75"];
const CYBERPUNK_OTHER = "#5b6b84";

export const THEMES = {
  cyberpunk: {
    id: "cyberpunk",
    label: "Cyberpunk",
    mode: "dark",
    decorativeEffects: true,
    chartPalette: CYBERPUNK_PALETTE,
    chartOtherColor: CYBERPUNK_OTHER,
  },
  daylight: {
    id: "daylight",
    label: "Daylight",
    mode: "light",
    decorativeEffects: false,
    // validated: node validate_palette.js "..." --mode light --surface "#f4f6fa"
    chartPalette: ["#009c78", "#938a00", "#ed00ff", "#ff3600", "#0090cc", "#009f00", "#a800ff", "#ff00ae"],
    chartOtherColor: "#5c6470",
  },
  slate: {
    id: "slate",
    label: "Slate",
    mode: "dark",
    decorativeEffects: false,
    // validated: node validate_palette.js "..." --mode dark --surface "#14181f"
    chartPalette: CYBERPUNK_PALETTE,
    chartOtherColor: CYBERPUNK_OTHER,
  },
  contrast: {
    id: "contrast",
    label: "Contrast",
    mode: "dark",
    decorativeEffects: false,
    // validated: node validate_palette.js "..." --mode dark --surface "#000000"
    chartPalette: CYBERPUNK_PALETTE,
    chartOtherColor: CYBERPUNK_OTHER,
  },
};

export const DEFAULT_THEME = "cyberpunk";
export const STORAGE_KEY = "ci_theme";

export const isValidTheme = (id) => Object.prototype.hasOwnProperty.call(THEMES, id);

// localStorage can throw (private-browsing storage blocks, quota, disabled
// cookies/storage) — every access here is wrapped so a blocked browser just
// falls back to the default theme instead of erroring (FR-011).
export function readLocalTheme() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !isValidTheme(parsed.theme) || typeof parsed.updatedAt !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

export function writeLocalTheme(theme, updatedAt = new Date().toISOString()) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ theme, updatedAt }));
  } catch {
    // storage unavailable — the theme still applies for this page load,
    // it just won't be remembered next time (FR-011)
  }
  return updatedAt;
}

let currentTheme = DEFAULT_THEME;
export const getCurrentTheme = () => currentTheme;

// Sets data-theme (drives every [data-theme="..."] block in style.css) and
// swaps the chart categorical palette in place — every chart-rendering file
// reads PALETTE/OTHER_COLOR by reference, so this is the only file that
// needs to know a theme changed.
//
// The light/dark signal is stored as document.documentElement's
// data-color-scheme, NOT body.dataset.mode — body.dataset.mode is already
// the app's own *navigation* mode (home/studio/modelling/portal/chat/
// account, set in state.js and read by router.js and the
// body[data-mode="..."] CSS layout rules). Reusing that name here would
// silently clobber it on every theme switch and break the sidebar/layout.
export function applyTheme(id) {
  const theme = THEMES[id] || THEMES[DEFAULT_THEME];
  currentTheme = theme.id;
  document.documentElement.dataset.theme = theme.id;
  document.documentElement.dataset.colorScheme = theme.mode;
  PALETTE.splice(0, PALETTE.length, ...theme.chartPalette);
  setOtherColor(theme.chartOtherColor);
  return theme.id;
}

// Boot-time sync: the inline bootstrap <script> in index.html's <head>
// already set data-theme on <html> (before first paint, from the same
// localStorage key), but as a non-module script it can't reach PALETTE/
// OTHER_COLOR — call this once during app init so the chart palette matches
// whatever theme is already showing, instead of defaulting to cyberpunk's
// until the user happens to touch the picker.
export function initTheme() {
  const local = readLocalTheme();
  applyTheme(local ? local.theme : DEFAULT_THEME);
}

// User-driven selection (the theme picker): apply + remember locally, and
// push to the account (FR-005) rather than waiting for the next boot's
// reconcileTheme() — otherwise a second device wouldn't see this choice
// until this browser happened to reload. The push is fire-and-forget and
// safe to attempt unconditionally: the picker only exists inside the
// post-login app shell, so this always runs authenticated in practice; if
// the session has since expired, the resulting 401 goes through the same
// api()-driven re-auth flow every other authenticated action already uses.
export function selectTheme(id) {
  const theme = applyTheme(id);
  const updatedAt = writeLocalTheme(theme);
  pushLocalToAccount({ theme, updatedAt });
  return { theme, updatedAt };
}

// Reconcile the browser-local preference against the signed-in user's
// account-level one (FR-006): whichever was chosen more recently wins, and
// is written back to whichever side lost, so the two converge. Safe to call
// on every authenticated boot/login — a network failure or a brand-new
// account with nothing set on either side just leaves things as they are
// (FR-012: sync failures never block the locally-selected theme).
export async function reconcileTheme() {
  let account;
  try {
    account = await api("/api/users/me/theme");
  } catch {
    return; // offline/failed — the local theme already applied, nothing to sync
  }
  const local = readLocalTheme();

  if (!account.theme && !local) return;        // nothing recorded anywhere yet
  if (!account.theme) {                          // local-only so far — push it up
    await pushLocalToAccount(local);
    return;
  }
  if (!local) {                                   // account-only so far — pull it down
    applyTheme(account.theme);
    writeLocalTheme(account.theme, account.updated_at);
    return;
  }

  // both exist — most recent timestamp wins (ISO-8601 UTC strings compare
  // correctly as dates; server-stamped for account, client-stamped for local)
  if (new Date(local.updatedAt) >= new Date(account.updated_at)) {
    if (local.theme !== account.theme) await pushLocalToAccount(local);
    applyTheme(local.theme);
  } else {
    applyTheme(account.theme);
    writeLocalTheme(account.theme, account.updated_at);
  }
}

async function pushLocalToAccount(local) {
  try {
    await api("/api/users/me/theme", { method: "PUT", body: { theme: local.theme } });
  } catch {
    // account sync failed (offline, expired session, etc.) — the local
    // theme already applies; this will simply be retried on the next
    // reconcileTheme() call (FR-012)
  }
}
