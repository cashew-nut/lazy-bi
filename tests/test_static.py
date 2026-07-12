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
    assert 'from "./completion.js"' in lab
    assert 'from "./completion.js"' in editor
    # the vocabulary now lives only in the shared module, not duplicated in the lab
    assert client.get("/static/js/completion.js").status_code == 200
    assert "const DSL_FUNCTIONS" not in lab
    assert "const DSL_FUNCTIONS" in client.get("/static/js/completion.js").text
