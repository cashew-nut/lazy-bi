/* Model memory curation (admin): the pool of facts the chat assistant has
   learned about a semantic model — undeclared synonyms and vocabulary
   notes, stored against the model (never the user) and merged into every
   future conversation's catalog. Admins review, edit, and delete anything
   the assistant recorded, and can add facts by hand; the server enforces
   the role, this module is presentation. Opened from a model card in the
   Modelling workspace. */
"use strict";

import { $, api, el } from "./lib.js";

function close() {
  const overlay = $("#memories-modal");
  overlay.hidden = true;
  overlay.innerHTML = "";
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#memories-modal").hidden) close();
});

export async function openMemoriesModal(model) {
  const overlay = $("#memories-modal");
  overlay.innerHTML = "";

  const closeBtn = el("button", { class: "btn", onclick: close }, "✕ CLOSE");
  const list = el("div", { class: "mem-list" });
  const status = el("span", { class: "acct-meta", id: "mem-status" });

  const card = el("div", { class: "mm-card mem-card" },
    el("div", { class: "chart-head" },
      el("span", { class: "editor-file" }, `${model.label || model.name} — assistant memory`),
      el("span", { style: "flex:1" }), closeBtn),
    el("div", { class: "empty-note" },
      "facts the chat assistant learned about this model — synonyms and notes it feeds back "
      + "into every future conversation, for every user. nothing here is (or may be) about a "
      + "specific user."),
    list,
    addForm(model, list, status),
    status);

  overlay.append(card);
  overlay.hidden = false;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  await refresh(model, list, status);
}

async function refresh(model, list, status) {
  list.innerHTML = "";
  let memories;
  try {
    memories = await api(`/api/models/${model.name}/memories`);
  } catch (err) {
    list.append(el("div", { class: "empty-note" }, "✕ " + err.message));
    return;
  }
  if (!memories.length) {
    list.append(el("div", { class: "empty-note" }, "no memories yet — the assistant records them as it chats"));
  }
  for (const m of memories) list.append(memoryRow(model, m, list, status));
}

function memoryRow(model, m, list, status) {
  const content = el("input", { class: "mem-content", value: m.content, spellcheck: "false" });
  const save = el("button", { class: "mini-btn", title: "save the edited text" }, "✓ save");
  save.hidden = true;
  content.addEventListener("input", () => { save.hidden = content.value.trim() === m.content; });
  save.addEventListener("click", async () => {
    status.textContent = "";
    try {
      await api(`/api/models/${model.name}/memories/${m.id}`,
        { method: "PATCH", body: { content: content.value.trim() } });
      await refresh(model, list, status);
    } catch (err) {
      status.textContent = "✕ " + err.message;
    }
  });
  const del = el("button", { class: "mini-btn", title: "forget this memory" }, "✕ forget");
  del.addEventListener("click", async () => {
    status.textContent = "";
    try {
      await api(`/api/models/${model.name}/memories/${m.id}`, { method: "DELETE" });
      await refresh(model, list, status);
    } catch (err) {
      status.textContent = "✕ " + err.message;
    }
  });
  return el("div", { class: "acct-row mem-row" },
    el("span", { class: "tag mem-kind" }, m.kind.toUpperCase()),
    el("span", { class: "acct-name mem-subject" }, m.kind === "synonym" ? `${m.subject} ←` : ""),
    content,
    el("span", { class: "acct-meta", title: `recorded by ${m.created_by || "unknown"}` },
      `${m.source} · ${(m.created_at || "").slice(0, 10)}`),
    save, del);
}

function addForm(model, list, status) {
  const kind = el("select", { class: "mem-new-kind", title: "synonym: an alternate term for a declared field · note: a free-text fact about the model" },
    el("option", { value: "synonym" }, "synonym"),
    el("option", { value: "note" }, "note"));
  const subject = el("select", { class: "mem-new-subject", title: "the declared dimension/measure the synonym maps to" });
  for (const d of model.dimensions || []) subject.append(el("option", { value: d.name }, `${d.name} (dim)`));
  for (const ms of model.measures || []) subject.append(el("option", { value: ms.name }, `${ms.name} (msr)`));
  const content = el("input", { class: "mem-new-content", placeholder: "the term users say", spellcheck: "false" });
  kind.addEventListener("change", () => {
    subject.hidden = kind.value !== "synonym";
    content.placeholder = kind.value === "synonym" ? "the term users say" : "a short fact about this model's data";
  });
  const add = el("button", { class: "btn alt" }, "+ ADD");
  add.addEventListener("click", async () => {
    status.textContent = "";
    if (!content.value.trim()) return;
    try {
      await api(`/api/models/${model.name}/memories`, {
        method: "POST",
        body: {
          kind: kind.value,
          subject: kind.value === "synonym" ? subject.value : "",
          content: content.value.trim(),
        },
      });
      content.value = "";
      await refresh(model, list, status);
    } catch (err) {
      status.textContent = "✕ " + err.message;
    }
  });
  return el("div", { class: "acct-row mem-add" }, kind, subject, content, add);
}
