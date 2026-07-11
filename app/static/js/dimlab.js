/* Common dimensional models in the UI:
     renderImportPanel() → panel in the fact-model editor that inserts a
                           dimension_imports block for a chosen bundle+dataset
   Bundles come from /api/dimensions and are deliberately kept out of the
   builder's model <select> — they are dimension providers, not queryable.
   The sidebar bundle list now lives in the Modelling workspace (modelling.js). */
"use strict";

import { insertAtCursor } from "./editor.js";
import { $, api, el } from "./lib.js";
import { state } from "./state.js";

// build a dimension_imports entry; if the model already has the block, add
// just the list item, otherwise emit the header too
function importSnippet(hasBlock, bundleName, dataset) {
  const onGuess = dataset.dimensions[0] || "key";
  const item =
    `  - bundle: ${bundleName}\n` +
    `    anchor_dataset: ${dataset.name}\n` +
    `    on: ${onGuess}   # this model's column that matches ${bundleName}.${dataset.name}\n`;
  return hasBlock ? item : `\ndimension_imports:\n${item}`;
}

export async function renderImportPanel() {
  const panel = $("#editor-imports");
  if (!state.bundles.length) state.bundles = await api("/api/dimensions");
  panel.innerHTML = "";
  panel.append(el("div", { class: "sec-title" }, "Common Dimensions"));
  if (!state.bundles.length) {
    panel.append(el("div", { class: "empty-note" }, "no common models yet — create one from the sidebar"));
    return;
  }
  panel.append(el("div", { class: "empty-note" }, "click a dataset to import it into this model"));
  for (const b of state.bundles) {
    const card = el("div", { class: "import-card" });
    card.append(el("div", { class: "nm" }, b.label));
    const chips = el("div", { class: "import-datasets" });
    for (const ds of b.datasets) {
      const chip = el("div", { class: "col-chip", title: `import anchored on ${ds.name}` },
        el("span", {}, ds.name),
        el("span", { class: "dt" }, `${ds.dimensions.length} dim${ds.dimensions.length === 1 ? "" : "s"}`));
      chip.addEventListener("click", () => {
        const ta = $("#yaml-editor");
        const hasBlock = /^dimension_imports:/m.test(ta.value);
        insertAtCursor(ta, importSnippet(hasBlock, b.name, ds));
      });
      chips.append(chip);
    }
    card.append(chips);
    panel.append(card);
  }
}
