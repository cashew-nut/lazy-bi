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
