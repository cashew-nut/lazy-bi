"""Instant mode's dashboard surface (specs/016-instant-cross-filter/).

The persisted opt-in flag, and the static-asset guards that keep the promise
the constitution exception was granted on: Perspective is vendored, headless,
and loaded by nothing but an instant dashboard.
"""
import json


# ── the persisted opt-in ────────────────────────────────────────────────

def test_dashboards_default_to_not_instant(client):
    """SC-003: every dashboard that existed before this feature, and every
    caller that doesn't know about the field, keeps today's behavior."""
    dash = client.post("/api/dashboards", json={"name": "plain", "items": []}).json()
    assert dash["instant"] is False
    assert client.get(f"/api/dashboards/{dash['id']}").json()["instant"] is False


def test_instant_flag_round_trips(client):
    created = client.post("/api/dashboards",
                          json={"name": "fast", "items": [], "instant": True}).json()
    assert created["instant"] is True
    # cold read, not the create response
    assert client.get(f"/api/dashboards/{created['id']}").json()["instant"] is True
    assert any(d["instant"] for d in client.get("/api/dashboards").json() if d["id"] == created["id"])


def test_instant_flag_can_be_turned_off_again(client):
    dash = client.post("/api/dashboards",
                       json={"name": "toggler", "items": [], "instant": True}).json()
    updated = client.put(f"/api/dashboards/{dash['id']}",
                         json={"name": "toggler", "items": [], "instant": False}).json()
    assert updated["instant"] is False
    assert client.get(f"/api/dashboards/{dash['id']}").json()["instant"] is False


def test_legacy_dashboard_rows_read_as_not_instant(client):
    """Rows written before this feature have no `instant` key at all — they
    must read as False rather than blowing up or defaulting to on."""
    from app.registry import registry

    dash = client.post("/api/dashboards", json={"name": "legacy", "items": []}).json()
    with registry.store._conn() as conn:
        conn.execute("UPDATE dashboards SET items = ? WHERE id = ?",
                     (json.dumps({"items": [], "views": [{"name": "default", "filters": []}],
                                  "active_view": 0}), dash["id"]))
    assert client.get(f"/api/dashboards/{dash['id']}").json()["instant"] is False


# ── vendored assets ─────────────────────────────────────────────────────

def test_perspective_is_vendored_not_cdn_loaded(client):
    """FR-011. The layout matters as much as the presence: the ES module
    resolves its server wasm as ../wasm/… relative to itself, so cdn/ and
    wasm/ have to stay siblings."""
    for path in ("/static/vendor/perspective/cdn/perspective.js",
                 "/static/vendor/perspective/wasm/perspective-server.wasm",
                 "/static/vendor/perspective/wasm/perspective-js.wasm",
                 "/static/vendor/perspective/LICENSE.md"):
        assert client.get(path).status_code == 200, f"{path} is not being served"


def test_no_frontend_module_references_a_cdn(client):
    """The other half of R4: vendoring is pointless if something still
    reaches out at runtime. Nothing in app/static/ may load a remote asset."""
    import re
    from pathlib import Path

    from app import main

    offenders = []
    for path in [*(Path(main.STATIC_DIR) / "js").rglob("*.js"), Path(main.STATIC_DIR) / "index.html"]:
        for match in re.finditer(r"""["'(]https?://[^"')\s]+""", path.read_text()):
            url = match.group(0)[1:]
            if "www.w3.org" in url:      # SVG/XML namespaces, not fetched
                continue
            offenders.append(f"{path.name}: {url}")
    assert not offenders, f"remote asset references found: {offenders}"


def test_perspective_is_used_headlessly(client):
    """FR-004: aggregation only. If perspective-viewer (or any Perspective
    rendering component) ever shows up, the constitution exception this
    feature was granted no longer describes what the code does."""
    src = client.get("/static/js/instant.js").text
    assert "perspective-viewer" not in src
    assert ".view(" in src and "group_by" in src      # Table/View, the aggregation API
    for renderer in ("bar", "line", "pivot", "table", "scatter", "sankey", "geo", "ribbon", "stat"):
        chart = client.get(f"/static/js/charts/{renderer}.js")
        if chart.status_code == 200:
            assert "perspective" not in chart.text.lower(), \
                f"charts/{renderer}.js must not know instant mode exists (SC-005)"


def test_only_the_dashboard_loads_the_instant_engine(client):
    """FR-014: the Builder, Explorer, Notebook composer and Sandbox are out of
    scope, and the import is dynamic so nothing loads it on boot."""
    dashboard = client.get("/static/js/dashboard.js").text
    assert 'import("./instant.js")' in dashboard, "the engine must be lazily imported"
    assert 'from "./instant.js"' not in dashboard, "a static import would load it on boot"
    for module in ("builder", "explorer", "notebook", "composer", "sandbox", "main", "chat"):
        res = client.get(f"/static/js/{module}.js")
        if res.status_code == 200:
            assert "instant.js" not in res.text, f"{module}.js must not load the instant engine"


def test_extract_endpoint_is_only_reached_from_the_dashboard(client):
    """FR-014, the other half: no other view may call the extract endpoint."""
    from pathlib import Path

    from app import main

    callers = [p.name for p in (Path(main.STATIC_DIR) / "js").rglob("*.js")
               if "/api/query/extract" in p.read_text()]
    assert callers == ["dashboard.js"], f"unexpected extract callers: {callers}"
