"""Pipeline definitions: yaml parsing/validation (foundational — T003/T006),
materialization + run lifecycle (US1/US2), lineage (US3). See
specs/014-polars-pipeline-module/.
"""
import tempfile
from pathlib import Path

import pytest

from app import pipelines

VALID = """
name: silver_orders
label: Silver Orders
sources:
  - name: raw_orders
    format: parquet
    path: s3://b/bronze/orders/*.parquet
target:
  path: s3://b/silver/orders
  format: delta
materialization:
  mode: replace
script: |
  output = sources["raw_orders"]
"""


def test_parse_minimal_pipeline():
    p = pipelines.parse_pipeline_text(VALID)
    assert p.name == "silver_orders"
    assert list(p.sources) == ["raw_orders"]
    assert p.target.path == "s3://b/silver/orders"
    assert p.materialization.mode == "replace"
    assert p.timeout_seconds == 600


def test_invalid_yaml_rejected():
    with pytest.raises(pipelines.PipelineError):
        pipelines.parse_pipeline_text("name: [unclosed")


def test_yaml_must_be_mapping():
    with pytest.raises(pipelines.PipelineError):
        pipelines.parse_pipeline_text("- just\n- a\n- list\n")


@pytest.mark.parametrize("key", ["name", "sources", "target", "materialization", "script"])
def test_missing_required_key_rejected(key):
    import yaml as _yaml

    raw = _yaml.safe_load(VALID)
    del raw[key]
    with pytest.raises(pipelines.PipelineError):
        pipelines._parse_pipeline(raw, Path("<test>"))


def test_needs_at_least_one_source():
    text = VALID.replace(
        "sources:\n  - name: raw_orders\n    format: parquet\n    path: s3://b/bronze/orders/*.parquet\n",
        "sources: []\n",
    )
    with pytest.raises(pipelines.PipelineError, match="at least one source"):
        pipelines.parse_pipeline_text(text)


def test_duplicate_source_name_rejected():
    text = VALID.replace(
        "sources:\n  - name: raw_orders\n    format: parquet\n    path: s3://b/bronze/orders/*.parquet\n",
        "sources:\n  - name: raw_orders\n    format: parquet\n    path: s3://b/bronze/orders/*.parquet\n"
        "  - name: raw_orders\n    format: csv\n    path: s3://b/bronze/orders2.csv\n",
    )
    with pytest.raises(pipelines.PipelineError, match="duplicate source name"):
        pipelines.parse_pipeline_text(text)


def test_unsupported_source_format_rejected():
    text = VALID.replace("format: parquet\n    path: s3://b/bronze/orders/*.parquet",
                          "format: json\n    path: s3://b/bronze/orders/*.json")
    with pytest.raises(pipelines.PipelineError, match="unsupported format"):
        pipelines.parse_pipeline_text(text)


def test_unsupported_target_format_rejected():
    text = VALID.replace("format: delta", "format: csv")
    with pytest.raises(pipelines.PipelineError, match="unsupported format"):
        pipelines.parse_pipeline_text(text)


def test_script_syntax_error_rejected():
    text = VALID.replace('output = sources["raw_orders"]', "output = (")
    with pytest.raises(pipelines.PipelineError, match="invalid script syntax"):
        pipelines.parse_pipeline_text(text)


def test_empty_script_rejected():
    text = VALID.replace('  output = sources["raw_orders"]\n', "")
    with pytest.raises(pipelines.PipelineError, match="non-empty"):
        pipelines.parse_pipeline_text(text)


# --- materialization cross-rules -------------------------------------------

def test_upsert_requires_delta_target():
    text = VALID.replace("mode: replace", "mode: upsert\n  keys: [order_id]").replace("format: delta", "format: parquet")
    with pytest.raises(pipelines.PipelineError, match="upsert mode requires target.format 'delta'"):
        pipelines.parse_pipeline_text(text)


def test_upsert_requires_keys():
    text = VALID.replace("mode: replace", "mode: upsert")
    with pytest.raises(pipelines.PipelineError, match="requires 'keys'"):
        pipelines.parse_pipeline_text(text)


def test_upsert_valid_with_keys():
    text = VALID.replace("mode: replace", "mode: upsert\n  keys: [order_id]")
    p = pipelines.parse_pipeline_text(text)
    assert p.materialization.mode == "upsert"
    assert p.materialization.keys == ["order_id"]
    assert p.materialization.on_delete == "ignore"


def test_unsupported_on_delete_rejected():
    text = VALID.replace("mode: replace", "mode: upsert\n  keys: [order_id]\n  on_delete: nuke")
    with pytest.raises(pipelines.PipelineError, match="unsupported on_delete"):
        pipelines.parse_pipeline_text(text)


def test_soft_delete_requires_column():
    text = VALID.replace("mode: replace", "mode: upsert\n  keys: [order_id]\n  on_delete: soft_delete")
    with pytest.raises(pipelines.PipelineError, match="requires 'soft_delete_column'"):
        pipelines.parse_pipeline_text(text)


def test_soft_delete_valid():
    text = VALID.replace(
        "mode: replace",
        "mode: upsert\n  keys: [order_id]\n  on_delete: soft_delete\n  soft_delete_column: is_deleted",
    )
    p = pipelines.parse_pipeline_text(text)
    assert p.materialization.soft_delete_column == "is_deleted"


def test_predicate_requires_delete_predicate():
    text = VALID.replace("mode: replace", "mode: upsert\n  keys: [order_id]\n  on_delete: predicate")
    with pytest.raises(pipelines.PipelineError, match="requires 'delete_predicate'"):
        pipelines.parse_pipeline_text(text)


def test_predicate_valid():
    text = VALID.replace(
        "mode: replace",
        "mode: upsert\n  keys: [order_id]\n  on_delete: predicate\n  delete_predicate: \"region = 'EU'\"",
    )
    p = pipelines.parse_pipeline_text(text)
    assert p.materialization.delete_predicate == "region = 'EU'"


def test_timeout_bounds():
    with pytest.raises(pipelines.PipelineError, match="timeout_seconds"):
        pipelines.parse_pipeline_text(VALID + "timeout_seconds: 0\n")
    with pytest.raises(pipelines.PipelineError, match="timeout_seconds"):
        pipelines.parse_pipeline_text(VALID + "timeout_seconds: 999999\n")
    p = pipelines.parse_pipeline_text(VALID + "timeout_seconds: 60\n")
    assert p.timeout_seconds == 60


# --- lineage -----------------------------------------------------------------

def test_lineage_parses_and_validates_source_refs():
    text = VALID + (
        "lineage:\n"
        "  - field: order_id\n"
        "    from: [raw_orders.order_id]\n"
        "    transform: pass-through\n"
    )
    p = pipelines.parse_pipeline_text(text)
    assert p.lineage[0].field == "order_id"
    assert p.lineage[0].sources == ["raw_orders.order_id"]


def test_lineage_unknown_source_rejected():
    text = VALID + (
        "lineage:\n"
        "  - field: order_id\n"
        "    from: [nonexistent.order_id]\n"
    )
    with pytest.raises(pipelines.PipelineError, match="unknown source"):
        pipelines.parse_pipeline_text(text)


def test_lineage_duplicate_field_rejected():
    text = VALID + (
        "lineage:\n"
        "  - field: order_id\n"
        "    from: [raw_orders.order_id]\n"
        "  - field: order_id\n"
        "    from: [raw_orders.order_id]\n"
    )
    with pytest.raises(pipelines.PipelineError, match="duplicate entry"):
        pipelines.parse_pipeline_text(text)


# --- directory loading: layers + cross-pipeline validation -------------------

LAYERS_YAML = """
layers:
  - name: bronze
    label: Bronze
  - name: silver
"""


def test_load_layers_absent_directory_returns_empty(tmp_path):
    assert pipelines.load_layers(tmp_path) == {}


def test_load_layers_parses_ordered():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "layers.yaml").write_text(LAYERS_YAML)
    layers = pipelines.load_layers(tmp)
    assert list(layers) == ["bronze", "silver"]
    assert layers["bronze"].label == "Bronze"


def test_load_layers_empty_file_is_equivalent_to_absent(tmp_path):
    (tmp_path / "layers.yaml").write_text("# nothing here yet\n")
    assert pipelines.load_layers(tmp_path) == {}


def test_load_layers_duplicate_rejected(tmp_path):
    (tmp_path / "layers.yaml").write_text(LAYERS_YAML + "  - name: bronze\n")
    with pytest.raises(pipelines.PipelineError, match="duplicate layer"):
        pipelines.load_layers(tmp_path)


def test_load_pipelines_skips_layers_file(tmp_path):
    (tmp_path / "layers.yaml").write_text(LAYERS_YAML)
    (tmp_path / "one.yaml").write_text(VALID)
    loaded = pipelines.load_pipelines(tmp_path, pipelines.load_layers(tmp_path))
    assert list(loaded) == ["silver_orders"]


def test_load_pipelines_unknown_layer_reference_rejected(tmp_path):
    text = VALID.replace("path: s3://b/silver/orders", "path: s3://b/silver/orders\n  layer: nonexistent_layer")
    (tmp_path / "one.yaml").write_text(text)
    with pytest.raises(pipelines.PipelineError, match="unknown layer"):
        pipelines.load_pipelines(tmp_path, {})


def test_load_pipelines_layer_reference_ok_when_declared(tmp_path):
    (tmp_path / "layers.yaml").write_text(LAYERS_YAML)
    text = VALID.replace("path: s3://b/silver/orders", "path: s3://b/silver/orders\n  layer: silver")
    (tmp_path / "one.yaml").write_text(text)
    loaded = pipelines.load_pipelines(tmp_path, pipelines.load_layers(tmp_path))
    assert loaded["silver_orders"].target.layer == "silver"


def test_load_pipelines_duplicate_name_rejected(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    text2 = VALID.replace("path: s3://b/silver/orders", "path: s3://b/silver/orders2")
    (tmp_path / "two.yaml").write_text(text2)
    with pytest.raises(pipelines.PipelineError, match="duplicate pipeline name"):
        pipelines.load_pipelines(tmp_path, {})


def test_load_pipelines_duplicate_target_rejected(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    text2 = VALID.replace("name: silver_orders", "name: silver_orders_v2")
    (tmp_path / "two.yaml").write_text(text2)
    with pytest.raises(pipelines.PipelineError, match="already owned by pipeline"):
        pipelines.load_pipelines(tmp_path, {})


def test_load_pipelines_missing_directory_returns_empty(tmp_path):
    assert pipelines.load_pipelines(tmp_path / "nonexistent", {}) == {}


def test_layers_to_yaml_roundtrip(tmp_path):
    (tmp_path / "layers.yaml").write_text(LAYERS_YAML)
    loaded = pipelines.load_layers(tmp_path)
    rendered = pipelines.layers_to_yaml(loaded)
    (tmp_path / "layers.yaml").write_text(rendered)
    again = pipelines.load_layers(tmp_path)
    assert list(again) == list(loaded)


# --- PipelineStore run lifecycle --------------------------------------------


@pytest.fixture
def store(tmp_path):
    from app.pipelinestore import PipelineStore

    return PipelineStore(tmp_path / "test_pipelines.db")


def test_create_run_starts_queued(store):
    run = store.create_run("silver_orders", 1, "admin-tester")
    assert run["status"] == "queued"
    assert run["pipeline"] == "silver_orders"
    assert run["started_at"] is None


def test_run_lifecycle_to_succeeded(store):
    run = store.create_run("silver_orders", 1, "admin-tester")
    store.mark_running(run["id"])
    running = store.get_run(run["id"])
    assert running["status"] == "running"
    assert running["started_at"] is not None

    finished = store.finish_run(
        run["id"], "succeeded", rows_written=10, rows_deleted=0, rows_flagged=0,
        lineage_ok=True, output_schema=[{"name": "a", "dtype": "Int64"}],
    )
    assert finished["status"] == "succeeded"
    assert finished["rows_written"] == 10
    assert finished["lineage_ok"] is True
    assert finished["output_schema"] == [{"name": "a", "dtype": "Int64"}]


def test_finish_run_rejects_non_terminal_status(store):
    run = store.create_run("p", None, "system")
    with pytest.raises(ValueError):
        store.finish_run(run["id"], "running")


def test_pending_for_reflects_queued_and_running(store):
    assert store.pending_for("p") is None
    run = store.create_run("p", None, "system")
    assert store.pending_for("p")["id"] == run["id"]
    store.mark_running(run["id"])
    assert store.pending_for("p")["id"] == run["id"]
    store.finish_run(run["id"], "succeeded")
    assert store.pending_for("p") is None


def test_sweep_interrupted_marks_queued_and_running(store):
    q = store.create_run("p1", None, "system")
    r = store.create_run("p2", None, "system")
    store.mark_running(r["id"])
    done = store.create_run("p3", None, "system")
    store.finish_run(done["id"], "succeeded")

    count = store.sweep_interrupted()
    assert count == 2
    assert store.get_run(q["id"])["status"] == "interrupted"
    assert store.get_run(r["id"])["status"] == "interrupted"
    assert store.get_run(done["id"])["status"] == "succeeded"


def test_runs_for_orders_newest_first(store):
    first = store.create_run("p", None, "system")
    second = store.create_run("p", None, "system")
    runs = store.runs_for("p")
    assert [r["id"] for r in runs] == [second["id"], first["id"]]


def test_latest_successful_schema(store):
    assert store.latest_successful_schema("p") is None
    run = store.create_run("p", None, "system")
    store.finish_run(run["id"], "failed", error="boom")
    assert store.latest_successful_schema("p") is None
    run2 = store.create_run("p", None, "system")
    store.finish_run(run2["id"], "succeeded", output_schema=[{"name": "x", "dtype": "Int64"}])
    assert store.latest_successful_schema("p") == [{"name": "x", "dtype": "Int64"}]


def test_next_queued_is_fifo_across_pipelines(store):
    assert store.next_queued() is None
    a = store.create_run("pipeline_a", None, "system")
    store.create_run("pipeline_b", None, "system")
    nxt = store.next_queued()
    assert nxt["id"] == a["id"]


# --- run-lifecycle + materialization, end to end against the moto bucket ---
# (US1/US2 — T013). These trigger real pipeline runs through the API, each
# spawning the actual app.pipeline_runner subprocess against the shared moto
# server the app lifespan started (conftest.py's `client` fixture). Pipeline
# yaml files land in the real pipelines/ directory for the duration of the
# test and are deleted at the end — the same convention test_api.py already
# uses for temp models (e.g. test_editor_create_and_delete_model).

import time

SALES_SOURCE = "s3://cash-intel/sales/*.parquet"


def _poll_run(client, run_id: int, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not reach a terminal status within {timeout}s")


def _create_and_run(client, name: str, yaml_text: str) -> dict:
    created = client.post("/api/pipelines", json={"yaml": yaml_text})
    assert created.status_code == 201, created.text
    triggered = client.post(f"/api/pipelines/{name}/run")
    assert triggered.status_code == 202, triggered.text
    return _poll_run(client, triggered.json()["run_id"])


def _delete_pipeline(client, name: str) -> None:
    client.delete(f"/api/pipelines/{name}")


def test_replace_pipeline_delta_target_end_to_end(client):
    name = "test_pipe_replace_delta"
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
  output = sources["sales"].select(["order_id", "region"]).head(5)
"""
    try:
        run = _create_and_run(client, name, yaml_text)
        assert run["status"] == "succeeded", run
        assert run["rows_written"] == 5
        import polars as pl

        from app import config

        result = pl.scan_delta(target, storage_options=config.delta_write_options()).collect()
        assert result.height == 5
        assert set(result.columns) == {"order_id", "region"}
    finally:
        _delete_pipeline(client, name)


def test_replace_pipeline_parquet_target_end_to_end(client):
    name = "test_pipe_replace_parquet"
    target_key = f"pipeline_test/{name}.parquet"
    target = f"s3://cash-intel/{target_key}"
    yaml_text = f"""
name: {name}
sources:
  - name: sales
    format: parquet
    path: {SALES_SOURCE}
target:
  path: {target}
  format: parquet
materialization:
  mode: replace
script: |
  output = sources["sales"].select(["order_id"]).head(3)
"""
    try:
        run = _create_and_run(client, name, yaml_text)
        assert run["status"] == "succeeded", run
        assert run["rows_written"] == 3

        import polars as pl

        from app import config

        result = pl.scan_parquet(target, storage_options=config.storage_options()).collect()
        assert result.height == 3
    finally:
        _delete_pipeline(client, name)


def test_failing_script_leaves_target_intact(client):
    """A run whose script raises must not touch a target that already has
    good data from a prior successful run (SC-003)."""
    name = "test_pipe_fails_safely"
    target = f"s3://cash-intel/pipeline_test/{name}"
    good_yaml = f"""
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
  output = sources["sales"].select(["order_id"]).head(4)
"""
    try:
        run = _create_and_run(client, name, good_yaml)
        assert run["status"] == "succeeded"

        bad_yaml = good_yaml.replace(
            'output = sources["sales"].select(["order_id"]).head(4)',
            'raise ValueError("boom")',
        )
        updated = client.put(f"/api/pipelines/{name}/yaml", json={"yaml": bad_yaml})
        assert updated.status_code == 200, updated.text
        triggered = client.post(f"/api/pipelines/{name}/run")
        assert triggered.status_code == 202
        failed_run = _poll_run(client, triggered.json()["run_id"])
        assert failed_run["status"] == "failed"
        assert "boom" in failed_run["error"]

        import polars as pl

        from app import config

        result = pl.scan_delta(target, storage_options=config.delta_write_options()).collect()
        assert result.height == 4  # untouched by the failed run
    finally:
        _delete_pipeline(client, name)


def test_missing_output_variable_fails(client):
    name = "test_pipe_missing_output"
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
  x = 1
"""
    try:
        run = _create_and_run(client, name, yaml_text)
        assert run["status"] == "failed"
        assert "output" in run["error"]
    finally:
        _delete_pipeline(client, name)


def test_wrong_type_output_fails(client):
    name = "test_pipe_wrong_type_output"
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
  output = 42
"""
    try:
        run = _create_and_run(client, name, yaml_text)
        assert run["status"] == "failed"
        assert "LazyFrame" in run["error"] or "DataFrame" in run["error"]
    finally:
        _delete_pipeline(client, name)


def test_run_timeout_is_killed(client):
    name = "test_pipe_timeout"
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
timeout_seconds: 1
script: |
  import time
  time.sleep(10)
  output = sources["sales"].head(1)
"""
    try:
        run = _create_and_run(client, name, yaml_text)
        assert run["status"] == "timed_out"
    finally:
        _delete_pipeline(client, name)


def test_duplicate_trigger_for_same_pipeline_refused(client):
    """A pipeline that already has a queued/running row refuses a second
    trigger (I1 remediation) — a *different* pipeline may still queue."""
    name = "test_pipe_duplicate_trigger"
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
timeout_seconds: 5
script: |
  import time
  time.sleep(2)
  output = sources["sales"].head(1)
"""
    try:
        created = client.post("/api/pipelines", json={"yaml": yaml_text})
        assert created.status_code == 201
        first = client.post(f"/api/pipelines/{name}/run")
        assert first.status_code == 202
        second = client.post(f"/api/pipelines/{name}/run")
        assert second.status_code == 409
        _poll_run(client, first.json()["run_id"])
    finally:
        _delete_pipeline(client, name)


# --- upsert materialization matrix (US2 — T020, SC-002) ---------------------
# Unit-level against app.materialize directly (local delta paths, no S3
# needed — deltalake/polars work the same way against a plain filesystem
# path), so the full policy x change-type matrix runs fast and asserts exact
# target state without subprocess/moto overhead.

import polars as pl

from app.materialize import MaterializeError, materialize
from app.pipelines import Materialization, Target


def _seed(tmp_path, name: str, df: pl.DataFrame) -> Target:
    target = Target(path=str(tmp_path / name), format="delta")
    materialize(df, target, Materialization(mode="replace"), {})
    return target


@pytest.mark.parametrize("policy", ["ignore", "sync", "soft_delete", "predicate"])
def test_upsert_update_and_insert_for_every_policy(tmp_path, policy):
    """Given a target seeded with ids 1/2/3, an upsert output touching id=2
    (update) and id=4 (insert) — the update/insert half of the matrix must
    hold identically regardless of delete policy."""
    kwargs = {"mode": "upsert", "keys": ["id"], "on_delete": policy}
    if policy == "soft_delete":
        kwargs["soft_delete_column"] = "is_deleted"
    if policy == "predicate":
        kwargs["delete_predicate"] = "id = -1"  # matches nothing here — isolates update/insert
    mat = Materialization(**kwargs)

    target = Target(path=str(tmp_path / f"t_{policy}"), format="delta")
    if policy == "soft_delete":
        # soft_delete's flag column must exist from the target's creation —
        # seed via the same upsert config so the first-run branch adds it,
        # rather than a plain replace (see the retrofit-guard test).
        materialize(pl.DataFrame({"id": [1, 2, 3], "v": ["a", "b", "c"]}), target, mat, {})
    else:
        materialize(pl.DataFrame({"id": [1, 2, 3], "v": ["a", "b", "c"]}), target,
                    Materialization(mode="replace"), {})

    stats = materialize(pl.DataFrame({"id": [2, 4], "v": ["B", "D"]}), target, mat, {})
    result = pl.read_delta(target.path, storage_options={}).sort("id")

    assert result.filter(pl.col("id") == 2)["v"][0] == "B"  # updated
    assert result.filter(pl.col("id") == 4)["v"][0] == "D"  # inserted
    if policy == "sync":
        assert result["id"].to_list() == [2, 4]  # 1, 3 removed (missing from output)
        assert stats["rows_deleted"] == 2
    else:
        assert result["id"].to_list() == [1, 2, 3, 4]  # 1, 3 left alone
        assert stats["rows_deleted"] == 0


def test_upsert_sync_deletes_rows_missing_from_output(tmp_path):
    target = _seed(tmp_path, "t_sync", pl.DataFrame({"id": [1, 2, 3], "v": ["a", "b", "c"]}))
    mat = Materialization(mode="upsert", keys=["id"], on_delete="sync")
    stats = materialize(pl.DataFrame({"id": [1], "v": ["A"]}), target, mat, {})
    result = pl.read_delta(target.path, storage_options={}).sort("id")
    assert result["id"].to_list() == [1]
    assert stats["rows_deleted"] == 2


def test_upsert_soft_delete_flags_missing_rows_and_clears_on_reappearance(tmp_path):
    # seed via the soft_delete upsert itself (not a plain replace) — this is
    # the "first upsert run creates the target" path, which is what actually
    # gives the target its flag column from the start (see the retrofit
    # guard test below for the case where that didn't happen).
    target = Target(path=str(tmp_path / "t_soft"), format="delta")
    mat = Materialization(mode="upsert", keys=["id"], on_delete="soft_delete", soft_delete_column="is_deleted")
    materialize(pl.DataFrame({"id": [1, 2], "v": ["a", "b"]}), target, mat, {})

    stats1 = materialize(pl.DataFrame({"id": [2], "v": ["B"]}), target, mat, {})
    after1 = pl.read_delta(target.path, storage_options={}).sort("id")
    flags1 = dict(zip(after1["id"].to_list(), after1["is_deleted"].to_list()))
    assert flags1 == {1: True, 2: False}
    assert stats1["rows_flagged"] == 1

    stats2 = materialize(pl.DataFrame({"id": [1, 2], "v": ["A2", "B2"]}), target, mat, {})
    after2 = pl.read_delta(target.path, storage_options={}).sort("id")
    flags2 = dict(zip(after2["id"].to_list(), after2["is_deleted"].to_list()))
    assert flags2 == {1: False, 2: False}
    assert stats2["rows_flagged"] == 0


def test_upsert_soft_delete_retrofit_onto_existing_target_without_flag_column_fails(tmp_path):
    """A target already exists (e.g. created by `replace`) without the flag
    column; switching to soft_delete must fail clearly rather than let
    deltalake's merge-time schema evolution silently write null flags."""
    target = _seed(tmp_path, "t_retrofit", pl.DataFrame({"id": [1, 2], "v": ["a", "b"]}))
    mat = Materialization(mode="upsert", keys=["id"], on_delete="soft_delete", soft_delete_column="is_deleted")
    with pytest.raises(MaterializeError, match="missing the soft-delete column"):
        materialize(pl.DataFrame({"id": [2], "v": ["B"]}), target, mat, {})
    result = pl.read_delta(target.path, storage_options={}).sort("id")
    assert "is_deleted" not in result.columns  # untouched by the failed run


def test_upsert_predicate_deletes_matching_rows_before_merge(tmp_path):
    target = _seed(tmp_path, "t_pred", pl.DataFrame({"id": [1, 2, 3], "region": ["EU", "US", "EU"]}))
    mat = Materialization(mode="upsert", keys=["id"], on_delete="predicate", delete_predicate="region = 'EU'")
    stats = materialize(pl.DataFrame({"id": [4], "region": ["FR"]}), target, mat, {})
    result = pl.read_delta(target.path, storage_options={}).sort("id")
    assert result["id"].to_list() == [2, 4]  # 1, 3 matched the predicate and were removed
    assert stats["rows_deleted"] == 2


def test_upsert_first_run_against_missing_target_creates_it(tmp_path):
    target = Target(path=str(tmp_path / "brand_new"), format="delta")
    mat = Materialization(mode="upsert", keys=["id"], on_delete="soft_delete", soft_delete_column="is_deleted")
    stats = materialize(pl.DataFrame({"id": [1, 2], "v": ["a", "b"]}), target, mat, {})
    assert stats == {"rows_written": 2, "rows_deleted": 0, "rows_flagged": 0}
    result = pl.read_delta(target.path, storage_options={}).sort("id")
    assert result["is_deleted"].to_list() == [False, False]  # flag column present from the start


# --- upsert guards (run before any target modification) ---------------------

def test_guard_null_key_rejected(tmp_path):
    target = _seed(tmp_path, "g_null", pl.DataFrame({"id": [1], "v": ["a"]}))
    mat = Materialization(mode="upsert", keys=["id"])
    with pytest.raises(MaterializeError, match="null value"):
        materialize(pl.DataFrame({"id": [1, None], "v": ["a", "b"]}), target, mat, {})
    assert pl.read_delta(target.path, storage_options={}).height == 1  # untouched


def test_guard_duplicate_key_rejected(tmp_path):
    target = _seed(tmp_path, "g_dup", pl.DataFrame({"id": [1], "v": ["a"]}))
    mat = Materialization(mode="upsert", keys=["id"])
    with pytest.raises(MaterializeError, match="duplicate key"):
        materialize(pl.DataFrame({"id": [2, 2], "v": ["a", "b"]}), target, mat, {})
    assert pl.read_delta(target.path, storage_options={}).height == 1  # untouched


def test_guard_schema_mismatch_rejected(tmp_path):
    target = _seed(tmp_path, "g_schema", pl.DataFrame({"id": [1], "v": ["a"]}))
    mat = Materialization(mode="upsert", keys=["id"])
    with pytest.raises(MaterializeError, match="schema incompatible"):
        materialize(pl.DataFrame({"id": [2], "other": [1]}), target, mat, {})
    assert pl.read_delta(target.path, storage_options={}).height == 1  # untouched


def test_guard_empty_output_with_sync_halts_without_optin(tmp_path):
    target = _seed(tmp_path, "g_empty_sync", pl.DataFrame({"id": [1, 2], "v": ["a", "b"]}))
    mat = Materialization(mode="upsert", keys=["id"], on_delete="sync")
    empty = pl.DataFrame({"id": [], "v": []}, schema={"id": pl.Int64, "v": pl.String})
    with pytest.raises(MaterializeError, match="empty"):
        materialize(empty, target, mat, {})
    assert pl.read_delta(target.path, storage_options={}).height == 2  # untouched


def test_guard_empty_output_with_sync_optin_truncates(tmp_path):
    target = _seed(tmp_path, "g_empty_sync_optin", pl.DataFrame({"id": [1, 2], "v": ["a", "b"]}))
    mat = Materialization(mode="upsert", keys=["id"], on_delete="sync", allow_empty_sync=True)
    empty = pl.DataFrame({"id": [], "v": []}, schema={"id": pl.Int64, "v": pl.String})
    stats = materialize(empty, target, mat, {})
    assert stats["rows_deleted"] == 2
    assert pl.read_delta(target.path, storage_options={}).height == 0


# --- lineage helpers (US3 — T023/T026): validate_lineage, match_target_model,
# build_lineage_section ------------------------------------------------------

from app import semantic

LINEAGE_PIPELINE = VALID + (
    "lineage:\n"
    "  - field: order_id\n"
    "    from: [raw_orders.order_id]\n"
    "    transform: pass-through\n"
)


def test_validate_lineage_flags_declared_missing():
    p = pipelines.parse_pipeline_text(LINEAGE_PIPELINE)
    issues = pipelines.validate_lineage(p.lineage, [{"name": "other_col", "dtype": "Int64"}])
    assert {"kind": "declared_missing", "field": "order_id"} in issues


def test_validate_lineage_flags_undeclared_output_field():
    p = pipelines.parse_pipeline_text(LINEAGE_PIPELINE)
    issues = pipelines.validate_lineage(p.lineage, [
        {"name": "order_id", "dtype": "Int64"}, {"name": "extra_col", "dtype": "Int64"},
    ])
    assert {"kind": "undeclared_field", "field": "extra_col"} in issues
    assert not any(i["field"] == "order_id" for i in issues)


def test_validate_lineage_clean_when_matching():
    p = pipelines.parse_pipeline_text(LINEAGE_PIPELINE)
    issues = pipelines.validate_lineage(p.lineage, [{"name": "order_id", "dtype": "Int64"}])
    assert issues == []


def _model(text: str) -> semantic.Model:
    return semantic.parse_model_text(text)


def test_match_target_model_delta_exact_path():
    p = pipelines.parse_pipeline_text(VALID)  # target: s3://b/silver/orders, format delta
    model = _model(
        "name: gold\nsource: {format: delta, path: s3://b/silver/orders}\n"
        "dimensions: [{name: x}]\nmeasures: [{name: rows, expr: count()}]\n"
    )
    assert pipelines.match_target_model(p, {"gold": model}) == "gold"


def test_match_target_model_no_match():
    p = pipelines.parse_pipeline_text(VALID)
    model = _model(
        "name: unrelated\nsource: {format: parquet, path: s3://b/other/*.parquet}\n"
        "dimensions: [{name: x}]\nmeasures: [{name: rows, expr: count()}]\n"
    )
    assert pipelines.match_target_model(p, {"unrelated": model}) is None


def test_match_target_model_parquet_glob():
    text = VALID.replace("format: delta", "format: parquet").replace(
        "path: s3://b/silver/orders", "path: s3://b/silver/orders.parquet")
    p = pipelines.parse_pipeline_text(text)
    model = _model(
        "name: gold\nsource: {format: parquet, path: s3://b/silver/*.parquet}\n"
        "dimensions: [{name: x}]\nmeasures: [{name: rows, expr: count()}]\n"
    )
    assert pipelines.match_target_model(p, {"gold": model}) == "gold"


def test_build_lineage_section_renders_layer_qualified_refs():
    text = LINEAGE_PIPELINE.replace(
        "    path: s3://b/bronze/orders/*.parquet\n",
        "    path: s3://b/bronze/orders/*.parquet\n    layer: bronze\n",
    )
    p = pipelines.parse_pipeline_text(text)
    section = pipelines.build_lineage_section(
        p, [{"name": "order_id", "dtype": "Int64"}], [], "2026-07-17T00:00:00Z"
    )
    assert section["fields"][0]["sources"] == ["bronze:raw_orders.order_id"]


def test_build_lineage_section_marks_stale():
    p = pipelines.parse_pipeline_text(LINEAGE_PIPELINE)
    issues = [{"kind": "declared_missing", "field": "order_id"}]
    section = pipelines.build_lineage_section(p, [], issues, "2026-07-17T00:00:00Z")
    assert section["fields"][0]["stale"] is True


def test_build_lineage_section_orphaned_flag():
    p = pipelines.parse_pipeline_text(LINEAGE_PIPELINE)
    section = pipelines.build_lineage_section(
        p, [{"name": "order_id", "dtype": "Int64"}], [], "t", orphaned=True
    )
    assert section["orphaned"] is True
