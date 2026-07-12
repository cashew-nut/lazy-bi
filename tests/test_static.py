"""Static asset serving: every response must force revalidation.

Regression guard for a stale-cache papercut — without an explicit
Cache-Control header, browsers fall back to heuristic caching and can go on
serving an old JS/CSS module long after the file on disk changed.
"""


def test_index_forces_revalidation(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_static_asset_forces_revalidation(client):
    res = client.get("/static/js/main.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_static_asset_not_modified_still_carries_header(client):
    first = client.get("/static/js/main.js")
    conditional = client.get("/static/js/main.js", headers={"if-none-match": first.headers["etag"]})
    assert conditional.status_code == 304
    assert conditional.headers["cache-control"] == "no-cache"


# ── 007-modelling-workspace: information-architecture move ──

def test_nav_renamed_data_to_modelling(client):
    html = client.get("/").text
    assert 'data-mode="modelling"' in html
    assert ">MODELLING<" in html
    assert ">DATA<" not in html


def test_studio_sidebar_has_no_authoring_controls(client):
    """The three authoring actions moved out of Studio into Modelling."""
    html = client.get("/").text
    for gone in ('id="edit-model"', 'id="new-model"', 'id="new-bundle"', 'id="bundle-list"'):
        assert gone not in html, f"{gone} should no longer be in the Studio sidebar"


def test_modelling_workspace_present(client):
    html = client.get("/").text
    assert 'id="modelling-view"' in html
    assert 'id="mk-new-model"' in html
    assert 'id="mk-new-bundle"' in html


def test_completion_engine_is_shared(client):
    """The measure lab and the model editor must use one completion engine —
    guards against the pre-extraction duplicate implementations."""
    lab = client.get("/static/js/measurelab.js").text
    editor = client.get("/static/js/editor.js").text
    modelform = client.get("/static/js/modelform.js").text
    assert 'from "./completion.js"' in lab
    assert 'from "./completion.js"' in editor
    assert 'from "./completion.js"' in modelform  # the guided form's measure rows too
    # the vocabulary now lives only in the shared module, not duplicated in the lab
    assert client.get("/static/js/completion.js").status_code == 200
    assert "const DSL_FUNCTIONS" not in lab
    assert "const DSL_FUNCTIONS" in client.get("/static/js/completion.js").text


def test_completer_apply_dispatches_input_event(client):
    """Regression guard: makeCompleter.apply() sets textarea.value directly,
    which fires no native 'input' event. Consumers that only mirror a field's
    value inside an 'input' listener (e.g. modelform.js's measure rows) would
    silently keep a stale value after the user accepts a suggestion unless
    apply() dispatches one itself."""
    completion = client.get("/static/js/completion.js").text
    assert 'dispatchEvent(new Event("input"' in completion


# ── 009-visual-parameters: intellisense sees sibling measures + parameters ──

def test_completion_engine_recognizes_param_context(client):
    """The shared completion engine must classify a param(' trigger distinctly
    from col(' so callers can offer declared parameter names, not columns."""
    completion = client.get("/static/js/completion.js").text
    assert 'kind: "param"' in completion
    assert "param\\(" in completion  # the trigger regex


def test_param_suggestion_gated_on_caller_supplying_parameters(client):
    """param('') must not be suggested to callers that never have parameters
    to offer (the model yaml editor, the guided model form) — those measures
    can never reference one (FR-007), so suggesting it would just set up a
    rejected save. Gating happens in dslItems, not per-caller vocabulary."""
    completion = client.get("/static/js/completion.js").text
    assert "parameters && parameters.length" in completion
    editor = client.get("/static/js/editor.js").text
    modelform = client.get("/static/js/modelform.js").text
    # neither passes a 4th (parameters) argument to dslItems
    assert "dslItems(ctx, editor.columns, after)" in editor or "dslItems(pctx, editor.columns, after)" in editor
    assert "dslItems(ctx, exprColumns(), after)" in modelform


def test_measure_lab_completion_offers_sibling_measures_and_parameters(client):
    """The measure lab's completion pool must include sibling measure names
    (for window-mode bare identifiers) and pass the visual's declared
    parameters through to the shared engine — previously it only ever
    offered source columns, which is wrong the moment running_total()/lag()
    is used, and never surfaced param() completions at all."""
    lab = client.get("/static/js/measurelab.js").text
    assert "state.model.measures.map" in lab
    assert "state.inlineMeasures.map" in lab
    assert "dslItems(ctx, exprPool(), after, state.parameters)" in lab
