/* Session state + login view (spec 011). The server enforces every rule;
   this module only decides what to render: who is signed in, the login
   overlay when there is no session, and hiding controls outside the
   signed-in role (elements carry data-role="author"|"admin"). */
"use strict";

import { $, api } from "./lib.js";

let currentUser = null;

export const user = () => currentUser;
export const canAuthor = () => !!currentUser && (currentUser.role === "author" || currentUser.role === "admin");
export const isAdmin = () => !!currentUser && currentUser.role === "admin";

const ROLE_OK = { author: canAuthor, admin: isAdmin };

/* Hide everything the signed-in role cannot use. Re-run after login. */
export function applyRoleGates() {
  for (const el of document.querySelectorAll("[data-role]")) {
    const check = ROLE_OK[el.dataset.role];
    el.hidden = check ? !check() : false;
  }
}

function renderBadge() {
  const badge = $("#user-badge");
  if (!badge) return;
  badge.hidden = !currentUser;
  if (currentUser) {
    $("#user-name").textContent = `${currentUser.display_name} · ${currentUser.role.toUpperCase()}`;
  }
}

function showLogin() {
  $("#login-view").hidden = false;
  $("#app").hidden = true;
  $("#login-user").focus();
}

function hideLogin() {
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  $("#login-error").textContent = "";
  $("#login-password").value = "";
}

/* Resolve the session (or run the login flow) before the app boots.
   Returns the signed-in user. Also re-opens the overlay if any later
   request hits a 401 (session expired / account deactivated) — the app's
   in-memory state survives, so unsaved work is not destroyed. */
export async function initAuth() {
  wireLoginForm();
  window.addEventListener("auth-required", showLogin);
  try {
    currentUser = await api("/api/auth/me");
    hideLogin();   // #app starts hidden in the markup — reveal it
  } catch {
    currentUser = await waitForLogin();
  }
  renderBadge();
  applyRoleGates();
  $("#logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already dead */ }
    location.reload();   // clean slate → boots into the login view
  });
  return currentUser;
}

let loginResolve = null;

function waitForLogin() {
  showLogin();
  return new Promise((resolve) => { loginResolve = resolve; });
}

function wireLoginForm() {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#login-error").textContent = "";
    try {
      const res = await api("/api/auth/login", {
        method: "POST",
        body: { username: $("#login-user").value.trim(), password: $("#login-password").value },
      });
      currentUser = res.user;
      hideLogin();
      renderBadge();
      applyRoleGates();
      if (loginResolve) { loginResolve(currentUser); loginResolve = null; }
    } catch (err) {
      $("#login-error").textContent = err.message || "sign-in failed";
    }
  });
}
