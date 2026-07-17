"""Pipeline API surface via TestClient (specs/014-polars-pipeline-module/):
CRUD, conflicts, validate, run-trigger lifecycle, audit. Uses the shared
moto-backed `client` (admin) fixture from conftest.py — the same convention
test_api.py uses for temp models applies here: pipelines created in these
tests are deleted at the end.
"""
import time

SALES_SOURCE = "s3://cash-intel/sales/*.parquet"


def _yaml(name: str, target: str, mode: str = "replace", extra_mat: str = "") -> str:
    return f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: {mode}
{extra_mat}
script: |
  output = sources["sales"].select(["order_id"]).head(2)
"""


def _poll(client, run_id: int, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never reached a terminal status")


def test_create_get_update_delete_roundtrip(client):
    name = "test_api_roundtrip"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = _yaml(name, target)
    try:
        created = client.post("/api/pipelines", json={"yaml": yaml_text})
        assert created.status_code == 201, created.text
        assert created.json()["name"] == name

        listed = client.get("/api/pipelines").json()
        assert any(p["name"] == name for p in listed)

        fetched = client.get(f"/api/pipelines/{name}/yaml")
        assert fetched.status_code == 200
        assert fetched.json()["yaml"] == yaml_text

        updated_yaml = yaml_text.replace('head(2)', 'head(3)')
        updated = client.put(f"/api/pipelines/{name}/yaml", json={"yaml": updated_yaml})
        assert updated.status_code == 200
        assert client.get(f"/api/pipelines/{name}/yaml").json()["yaml"] == updated_yaml
    finally:
        assert client.delete(f"/api/pipelines/{name}").status_code == 204
    assert not any(p["name"] == name for p in client.get("/api/pipelines").json())


def test_create_duplicate_name_conflict(client):
    name = "test_api_dup_name"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}")
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 409
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_create_duplicate_target_conflict(client):
    name1, name2 = "test_api_dup_target_a", "test_api_dup_target_b"
    target = "s3://cash-intel/pipeline_test/shared_target"
    yaml1 = _yaml(name1, target)
    yaml2 = _yaml(name2, target)
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml1}).status_code == 201
        res = client.post("/api/pipelines", json={"yaml": yaml2})
        assert res.status_code == 409
        assert name1 in res.json()["detail"]
    finally:
        client.delete(f"/api/pipelines/{name1}")
        client.delete(f"/api/pipelines/{name2}")


def test_update_cannot_rename(client):
    name = "test_api_rename_probe"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}")
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        renamed = yaml_text.replace(f"name: {name}", "name: test_api_renamed")
        res = client.put(f"/api/pipelines/{name}/yaml", json={"yaml": renamed})
        assert res.status_code == 400
        assert "immutable" in res.json()["detail"]
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_delete_while_run_pending_conflict(client):
    name = "test_api_delete_pending"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}").replace(
        'script: |\n  output = sources["sales"].select(["order_id"]).head(2)',
        'timeout_seconds: 5\nscript: |\n  import time\n  time.sleep(2)\n  '
        'output = sources["sales"].head(1)',
    )
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        run = client.post(f"/api/pipelines/{name}/run")
        assert run.status_code == 202
        assert client.delete(f"/api/pipelines/{name}").status_code == 409
        _poll(client, run.json()["run_id"])
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_run_while_pending_for_same_pipeline_conflict(client):
    name = "test_api_run_pending"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}").replace(
        'script: |\n  output = sources["sales"].select(["order_id"]).head(2)',
        'timeout_seconds: 5\nscript: |\n  import time\n  time.sleep(2)\n  '
        'output = sources["sales"].head(1)',
    )
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        first = client.post(f"/api/pipelines/{name}/run")
        assert first.status_code == 202
        second = client.post(f"/api/pipelines/{name}/run")
        assert second.status_code == 409
        _poll(client, first.json()["run_id"])
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_validate_endpoint_ok_and_error(client):
    ok = client.post("/api/pipelines/validate", json={"yaml": _yaml(
        "probe_validate", "s3://cash-intel/pipeline_test/probe_validate")}).json()
    assert ok["ok"] and ok["pipeline"]["name"] == "probe_validate"

    bad = client.post("/api/pipelines/validate", json={"yaml": "name: x"}).json()
    assert not bad["ok"]
    assert bad["error"]


def test_run_trigger_polls_to_terminal_status_and_updates_pipeline_list(client):
    name = "test_api_run_lifecycle"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = _yaml(name, target)
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        triggered = client.post(f"/api/pipelines/{name}/run")
        assert triggered.status_code == 202
        assert triggered.json()["status"] == "queued"
        run = _poll(client, triggered.json()["run_id"])
        assert run["status"] == "succeeded"
        assert run["rows_written"] == 2

        listed = next(p for p in client.get("/api/pipelines").json() if p["name"] == name)
        assert listed["latest_run"]["id"] == run["id"]
        assert listed["latest_run"]["status"] == "succeeded"

        runs = client.get(f"/api/pipelines/{name}/runs").json()
        assert runs[0]["id"] == run["id"]
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_run_unknown_pipeline_is_404(client):
    assert client.post("/api/pipelines/does_not_exist/run").status_code == 404
    assert client.get("/api/pipelines/does_not_exist/yaml").status_code == 404
    assert client.delete("/api/pipelines/does_not_exist").status_code == 404


def test_get_unknown_run_is_404(client):
    assert client.get("/api/runs/999999999").status_code == 404


def test_audit_rows_recorded_for_mutations_and_runs(client):
    from app.registry import registry

    name = "test_api_audit_probe"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}")
    try:
        client.post("/api/pipelines", json={"yaml": yaml_text})
        client.put(f"/api/pipelines/{name}/yaml", json={"yaml": yaml_text})
        triggered = client.post(f"/api/pipelines/{name}/run")
        _poll(client, triggered.json()["run_id"])

        events = registry.auth_store.audit_events()
        actions = [e["action"] for e in events if e["target"] == name]
        assert "pipeline.create" in actions
        assert "pipeline.update" in actions
        assert "pipeline.run" in actions
    finally:
        client.delete(f"/api/pipelines/{name}")
        events = registry.auth_store.audit_events()
        assert any(e["action"] == "pipeline.delete" and e["target"] == name for e in events)


# --- lineage sync, staleness, orphan-marking (US3 — T026) -------------------

def _model_yaml(name: str, target_path: str, target_format: str = "delta") -> str:
    return (
        f"name: {name}\n"
        f"source: {{format: {target_format}, path: {target_path}}}\n"
        "dimensions:\n  - name: order_id\n"
        "measures:\n  - name: rows\n    expr: count()\n"
    )


def test_lineage_syncs_into_target_model_after_successful_run(client):
    pipe_name = "test_lineage_sync_pipe"
    model_name = "test_lineage_sync_model"
    target = f"s3://cash-intel/pipeline_test/{pipe_name}"
    pipe_yaml = f"""
name: {pipe_name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id", "region"]).head(2)
lineage:
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
  - field: region
    from: [sales.region]
    transform: pass-through
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": pipe_yaml}).status_code == 201
        assert client.post("/api/models", json={"yaml": _model_yaml(model_name, target)}).status_code == 201

        triggered = client.post(f"/api/pipelines/{pipe_name}/run")
        run = _poll(client, triggered.json()["run_id"])
        assert run["status"] == "succeeded"
        assert run["lineage_ok"] is True

        model = next(m for m in client.get("/api/models").json() if m["name"] == model_name)
        lineage = model["pipeline_lineage"]
        assert lineage["pipeline"] == pipe_name
        assert not lineage["orphaned"]
        fields = {f["field"]: f for f in lineage["fields"]}
        assert fields["order_id"]["sources"] == ["sales.order_id"]
        assert not fields["order_id"].get("stale")
    finally:
        client.delete(f"/api/pipelines/{pipe_name}")
        client.delete(f"/api/models/{model_name}")


def test_lineage_marks_stale_when_declared_field_disappears_from_output(client):
    pipe_name = "test_lineage_stale_pipe"
    model_name = "test_lineage_stale_model"
    target = f"s3://cash-intel/pipeline_test/{pipe_name}"
    good_yaml = f"""
name: {pipe_name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id", "region"]).head(2)
lineage:
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
  - field: region
    from: [sales.region]
    transform: pass-through
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": good_yaml}).status_code == 201
        assert client.post("/api/models", json={"yaml": _model_yaml(model_name, target)}).status_code == 201
        run1 = _poll(client, client.post(f"/api/pipelines/{pipe_name}/run").json()["run_id"])
        assert run1["lineage_ok"] is True

        bad_yaml = good_yaml.replace(
            'output = sources["sales"].select(["order_id", "region"]).head(2)',
            'output = sources["sales"].select(["order_id"]).head(2)',
        )
        assert client.put(f"/api/pipelines/{pipe_name}/yaml", json={"yaml": bad_yaml}).status_code == 200
        run2 = _poll(client, client.post(f"/api/pipelines/{pipe_name}/run").json()["run_id"])
        assert run2["status"] == "succeeded"  # data is still written despite the mismatch
        assert run2["lineage_ok"] is False
        assert any(i["field"] == "region" and i["kind"] == "declared_missing" for i in run2["lineage_issues"])

        model = next(m for m in client.get("/api/models").json() if m["name"] == model_name)
        fields = {f["field"]: f for f in model["pipeline_lineage"]["fields"]}
        assert fields["region"].get("stale") is True
    finally:
        client.delete(f"/api/pipelines/{pipe_name}")
        client.delete(f"/api/models/{model_name}")


def test_lineage_section_orphaned_on_pipeline_delete(client):
    pipe_name = "test_lineage_orphan_pipe"
    model_name = "test_lineage_orphan_model"
    target = f"s3://cash-intel/pipeline_test/{pipe_name}"
    pipe_yaml = f"""
name: {pipe_name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id"]).head(2)
lineage:
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": pipe_yaml}).status_code == 201
        assert client.post("/api/models", json={"yaml": _model_yaml(model_name, target)}).status_code == 201
        _poll(client, client.post(f"/api/pipelines/{pipe_name}/run").json()["run_id"])

        assert client.delete(f"/api/pipelines/{pipe_name}").status_code == 204

        model = next(m for m in client.get("/api/models").json() if m["name"] == model_name)
        assert model["pipeline_lineage"]["orphaned"] is True
    finally:
        client.delete(f"/api/pipelines/{pipe_name}")
        client.delete(f"/api/models/{model_name}")


# --- lineage-suggest endpoint ------------------------------------------------

def test_suggest_lineage_409_when_no_schema_available(client):
    name = "test_suggest_no_schema"
    yaml_text = _yaml(name, f"s3://cash-intel/pipeline_test/{name}")
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        assert client.get(f"/api/pipelines/{name}/lineage/suggest").status_code == 409
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_suggest_lineage_after_run_suggests_pass_through(client):
    name = "test_suggest_after_run"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id", "region"]).head(2)
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        _poll(client, client.post(f"/api/pipelines/{name}/run").json()["run_id"])
        res = client.get(f"/api/pipelines/{name}/lineage/suggest")
        assert res.status_code == 200
        suggestions = {s["field"]: s for s in res.json()["suggestions"]}
        assert suggestions["order_id"]["from"] == ["sales.order_id"]
        assert suggestions["region"]["from"] == ["sales.region"]
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_suggest_lineage_excludes_already_declared_fields(client):
    name = "test_suggest_declared"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id", "region"]).head(2)
lineage:
  - field: order_id
    from: [sales.order_id]
    transform: pass-through
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        _poll(client, client.post(f"/api/pipelines/{name}/run").json()["run_id"])
        res = client.get(f"/api/pipelines/{name}/lineage/suggest")
        fields = {s["field"] for s in res.json()["suggestions"]}
        assert "order_id" not in fields  # already declared
        assert "region" in fields
    finally:
        client.delete(f"/api/pipelines/{name}")


# --- layers CRUD -------------------------------------------------------------

def test_layers_crud_roundtrip(client):
    original = client.get("/api/lineage/layers").json()["layers"]
    try:
        res = client.put("/api/lineage/layers", json={"layers": [
            {"name": "bronze", "label": "Bronze"}, {"name": "silver"}, {"name": "gold"},
        ]})
        assert res.status_code == 200
        layers = client.get("/api/lineage/layers").json()["layers"]
        assert [l["name"] for l in layers] == ["bronze", "silver", "gold"]
        assert layers[0]["label"] == "Bronze"
        assert layers[1]["label"] == "Silver"  # auto-titled default
    finally:
        client.put("/api/lineage/layers",
                   json={"layers": [{"name": l["name"], "label": l["label"]} for l in original]})


def test_layers_removal_conflict_when_referenced(client):
    name = "test_layers_conflict_pipe"
    yaml_text = f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
    layer: bronze
target:
  path: s3://cash-intel/pipeline_test/{name}
  format: delta
  layer: silver
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id"]).head(1)
"""
    original = client.get("/api/lineage/layers").json()["layers"]
    try:
        assert client.put("/api/lineage/layers", json={"layers": [
            {"name": "bronze"}, {"name": "silver"}, {"name": "gold"},
        ]}).status_code == 200
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201

        res = client.put("/api/lineage/layers", json={"layers": [{"name": "bronze"}, {"name": "gold"}]})
        assert res.status_code == 409
        assert name in res.json()["detail"]
    finally:
        client.delete(f"/api/pipelines/{name}")
        client.put("/api/lineage/layers",
                   json={"layers": [{"name": l["name"], "label": l["label"]} for l in original]})


# --- lineage graph (US4 — T029) ----------------------------------------------

def test_lineage_graph_chain_and_field_hops(client):
    name_a, name_b = "test_graph_a", "test_graph_b"
    path_a = f"s3://cash-intel/pipeline_test/{name_a}_target"
    path_b = f"s3://cash-intel/pipeline_test/{name_b}_target"
    yaml_a = f"""
name: {name_a}
sources:
  - name: raw
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {path_a}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["raw"].select(["order_id"]).head(2)
lineage:
  - field: order_id
    from: [raw.order_id]
    transform: pass-through
"""
    yaml_b = f"""
name: {name_b}
sources:
  - name: upstream
    format: delta
    path: {path_a}
target:
  path: {path_b}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["upstream"].select(["order_id"]).head(2)
lineage:
  - field: order_id
    from: [upstream.order_id]
    transform: pass-through
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_a}).status_code == 201
        assert client.post("/api/pipelines", json={"yaml": yaml_b}).status_code == 201

        graph = client.get("/api/lineage/graph").json()
        node_ids = {n["id"] for n in graph["nodes"]}
        assert SALES_SOURCE in node_ids
        assert path_a in node_ids
        assert path_b in node_ids

        edge_pairs = {(e["source_id"], e["target_id"], e["pipeline"]) for e in graph["edges"]}
        assert (SALES_SOURCE, path_a, name_a) in edge_pairs
        assert (path_a, path_b, name_b) in edge_pairs

        hop = next(f for f in graph["field_lineage"] if f["node_id"] == path_b and f["field"] == "order_id")
        assert hop["upstream"] == [{"node_id": path_a, "field": "order_id"}]

        # path_a is both a target (of pipe A) and a source (of pipe B) — its
        # node must show up exactly once, not duplicated
        assert sum(1 for n in graph["nodes"] if n["id"] == path_a) == 1
    finally:
        client.delete(f"/api/pipelines/{name_a}")
        client.delete(f"/api/pipelines/{name_b}")


def test_lineage_graph_edge_status_reflects_latest_run(client):
    name = "test_graph_status"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = _yaml(name, target)
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        edge_before = next(e for e in client.get("/api/lineage/graph").json()["edges"] if e["pipeline"] == name)
        assert edge_before["status"] is None

        triggered = client.post(f"/api/pipelines/{name}/run")
        _poll(client, triggered.json()["run_id"])

        edge_after = next(e for e in client.get("/api/lineage/graph").json()["edges"] if e["pipeline"] == name)
        assert edge_after["status"] == "succeeded"
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_lineage_graph_layer_grouping(client):
    name = "test_graph_layers"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
    layer: bronze
target:
  path: {target}
  format: delta
  layer: silver
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id"]).head(1)
"""
    # union with whatever's already declared (the shipped demo pipelines
    # reference bronze/silver/gold) rather than replacing wholesale — this
    # test only needs bronze+silver to exist, never needs to shrink the set
    original = client.get("/api/lineage/layers").json()["layers"]
    try:
        existing = {l["name"] for l in original}
        merged = list(original) + [{"name": n} for n in ("bronze", "silver") if n not in existing]
        assert client.put("/api/lineage/layers", json={"layers": merged}).status_code == 200
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201

        graph = client.get("/api/lineage/graph").json()
        source_node = next(n for n in graph["nodes"] if n["id"] == SALES_SOURCE)
        target_node = next(n for n in graph["nodes"] if n["id"] == target)
        assert source_node["layer"] == "bronze"
        assert target_node["layer"] == "silver"
        assert {"bronze", "silver"} <= {l["name"] for l in graph["layers"]}
    finally:
        client.delete(f"/api/pipelines/{name}")
        client.put("/api/lineage/layers",
                   json={"layers": [{"name": l["name"], "label": l["label"]} for l in original]})


def test_lineage_graph_node_without_layer_is_null(client):
    """A pipeline that never assigns a layer still produces a valid graph —
    those nodes simply have layer: null (FR-020: layers are optional per
    pipeline, regardless of whether any layers are globally declared)."""
    name = "test_graph_no_layer"
    target = f"s3://cash-intel/pipeline_test/{name}"
    yaml_text = _yaml(name, target)
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_text}).status_code == 201
        res = client.get("/api/lineage/graph")
        assert res.status_code == 200
        target_node = next(n for n in res.json()["nodes"] if n["id"] == target)
        assert target_node["layer"] is None
    finally:
        client.delete(f"/api/pipelines/{name}")


def test_lineage_graph_handles_cycle_without_hanging(client):
    name_x, name_y = "test_graph_cycle_x", "test_graph_cycle_y"
    path_x = f"s3://cash-intel/pipeline_test/{name_x}"
    path_y = f"s3://cash-intel/pipeline_test/{name_y}"
    yaml_x = f"""
name: {name_x}
sources:
  - name: y
    format: delta
    path: {path_y}
target:
  path: {path_x}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["y"].head(1)
"""
    yaml_y = f"""
name: {name_y}
sources:
  - name: x
    format: delta
    path: {path_x}
target:
  path: {path_y}
  format: delta
materialization:
  mode: replace
script: |
  output = sources["x"].head(1)
"""
    try:
        assert client.post("/api/pipelines", json={"yaml": yaml_x}).status_code == 201
        assert client.post("/api/pipelines", json={"yaml": yaml_y}).status_code == 201
        res = client.get("/api/lineage/graph")
        assert res.status_code == 200
        edge_pairs = {(e["source_id"], e["target_id"]) for e in res.json()["edges"]}
        assert (path_y, path_x) in edge_pairs
        assert (path_x, path_y) in edge_pairs
    finally:
        client.delete(f"/api/pipelines/{name_x}")
        client.delete(f"/api/pipelines/{name_y}")
