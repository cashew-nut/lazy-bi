/* ACCOUNT view (spec 011): personal access tokens + password change for
   every signed-in role; the user-management panel renders for admins only.
   The server enforces all of it — this module is presentation. */
"use strict";

import { isAdmin, user } from "./auth.js";
import { $, api, el } from "./lib.js";
import { hooks } from "./state.js";
import { getCurrentTheme, selectTheme, THEMES } from "./theme.js";

const ROLES = ["viewer", "author", "admin"];

export async function loadAccount() {
  renderThemePicker();
  await Promise.all([renderTokens(), isAdmin() ? renderUsers() : Promise.resolve()]);
  $("#account-users-panel").hidden = !isAdmin();
}
hooks.loadAccount = loadAccount;

// ── appearance (spec 013) ────────────────────────────────────

function renderThemePicker() {
  const box = $("#theme-picker");
  box.innerHTML = "";
  const active = getCurrentTheme();
  for (const theme of Object.values(THEMES)) {
    const btn = el("button", { type: "button", role: "radio", "aria-checked": String(theme.id === active) },
      theme.label);
    btn.classList.toggle("on", theme.id === active);
    btn.dataset.themeId = theme.id;
    box.append(btn);
  }
}

function wireThemePicker() {
  $("#theme-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-theme-id]");
    if (!btn) return;
    selectTheme(btn.dataset.themeId);
    renderThemePicker();
  });
}

// ── my tokens ────────────────────────────────────────────────

async function renderTokens() {
  const box = $("#token-list");
  box.innerHTML = "";
  const tokens = await api("/api/tokens");
  if (!tokens.length) {
    box.append(el("div", { class: "empty-note" }, "no tokens — create one to call the API from scripts"));
  }
  for (const t of tokens) {
    const row = el("div", { class: "acct-row" + (t.revoked_at ? " dim" : "") },
      el("span", { class: "acct-name" }, t.name),
      el("span", { class: "acct-meta" },
        t.revoked_at ? `revoked ${t.revoked_at.slice(0, 10)}`
          : `created ${t.created_at.slice(0, 10)} · last used ${t.last_used_at ? t.last_used_at.slice(0, 10) : "never"}`));
    if (!t.revoked_at) {
      const rm = el("button", { class: "btn plain" }, "REVOKE");
      rm.addEventListener("click", async () => {
        await api(`/api/tokens/${t.id}`, { method: "DELETE" });
        renderTokens();
      });
      row.append(rm);
    }
    box.append(row);
  }
}

function wireTokenForm() {
  $("#token-create").addEventListener("click", async () => {
    const name = $("#token-name").value.trim();
    if (!name) return;
    const res = await api("/api/tokens", { method: "POST", body: { name } });
    $("#token-name").value = "";
    // the secret exists only in this response — show it once, with a copy
    const reveal = $("#token-reveal");
    reveal.hidden = false;
    reveal.innerHTML = "";
    reveal.append(
      el("span", { class: "acct-meta" }, "copy it now — it is never shown again: "),
      el("code", {}, res.token),
      el("button", { class: "btn plain", onclick: () => navigator.clipboard.writeText(res.token) }, "COPY"));
    renderTokens();
  });
}

// ── change my password ───────────────────────────────────────

function wirePasswordForm() {
  $("#pw-change").addEventListener("click", async () => {
    const status = $("#pw-status");
    status.textContent = "";
    try {
      await api("/api/auth/password", {
        method: "POST",
        body: { current_password: $("#pw-current").value, new_password: $("#pw-new").value },
      });
      $("#pw-current").value = $("#pw-new").value = "";
      status.textContent = "✓ password changed — other sessions signed out";
    } catch (err) {
      status.textContent = "✗ " + err.message;
    }
  });
}

// ── user management (admin only) ─────────────────────────────

async function renderUsers() {
  const box = $("#user-list");
  box.innerHTML = "";
  const users = await api("/api/users");
  for (const u of users) {
    const roleSel = el("select", {},
      ...ROLES.map((r) => {
        const o = el("option", { value: r }, r);
        if (r === u.role) o.selected = true;
        return o;
      }));
    roleSel.addEventListener("change", () => patchUser(u.id, { role: roleSel.value }));
    const toggle = el("button", { class: "btn plain" }, u.is_active ? "DEACTIVATE" : "REACTIVATE");
    toggle.addEventListener("click", () => patchUser(u.id, { is_active: !u.is_active }));
    const reset = el("button", { class: "btn plain" }, "RESET PW");
    reset.addEventListener("click", () => {
      const pw = prompt(`New password for ${u.username} (min 8 chars):`);
      if (pw) patchUser(u.id, { password: pw });
    });
    const me = user() && user().id === u.id;
    box.append(el("div", { class: "acct-row" + (u.is_active ? "" : " dim") },
      el("span", { class: "acct-name" }, u.username + (me ? " (you)" : "")),
      el("span", { class: "acct-meta" }, u.display_name),
      roleSel, toggle, reset));
  }
}

async function patchUser(id, body) {
  const status = $("#user-status");
  status.textContent = "";
  try {
    await api(`/api/users/${id}`, { method: "PATCH", body });
  } catch (err) {
    status.textContent = "✗ " + err.message;   // e.g. last-active-admin refusal
  }
  renderUsers();
}

function wireUserForm() {
  $("#user-create").addEventListener("click", async () => {
    const status = $("#user-status");
    status.textContent = "";
    try {
      await api("/api/users", {
        method: "POST",
        body: {
          username: $("#nu-username").value.trim(),
          display_name: $("#nu-display").value.trim(),
          role: $("#nu-role").value,
          password: $("#nu-password").value,
        },
      });
      $("#nu-username").value = $("#nu-display").value = $("#nu-password").value = "";
      renderUsers();
    } catch (err) {
      status.textContent = "✗ " + err.message;
    }
  });
}

export function attachAccount() {
  wireThemePicker();
  wireTokenForm();
  wirePasswordForm();
  wireUserForm();
}
