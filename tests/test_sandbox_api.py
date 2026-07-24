"""Sandbox notebook API surface via TestClient: CRUD, run (against the real
moto-backed bucket via the shared `client`/`admin_client` fixtures), convert-
to-pipeline, and audit. Role gating itself is swept exhaustively in
tests/test_role_matrix.py — these tests focus on behavior.
"""
SALES_SOURCE = "s3://cash-intel/sales/*.parquet"


def _cell(cell_id: str, source: str) -> dict:
    return {"id": cell_id, "source": source}


def test_create_get_update_delete_roundtrip(client):
    created = client.post("/api/sandbox/notebooks", json={
        "name": "scratch", "cells": [_cell("c1", "1 + 1")],
    })
    assert created.status_code == 201, created.text
    nb_id = created.json()["id"]
    try:
        listed = client.get("/api/sandbox/notebooks").json()
        assert any(n["id"] == nb_id for n in listed)
        assert "cells" not in next(n for n in listed if n["id"] == nb_id)

        fetched = client.get(f"/api/sandbox/notebooks/{nb_id}")
        assert fetched.status_code == 200
        assert fetched.json()["cells"] == [_cell("c1", "1 + 1")]

        updated = client.put(f"/api/sandbox/notebooks/{nb_id}", json={
            "name": "renamed", "cells": [_cell("c1", "2 + 2")],
        })
        assert updated.status_code == 200
        assert updated.json()["name"] == "renamed"
        assert client.get(f"/api/sandbox/notebooks/{nb_id}").json()["cells"][0]["source"] == "2 + 2"
    finally:
        assert client.delete(f"/api/sandbox/notebooks/{nb_id}").status_code == 204
    assert client.get(f"/api/sandbox/notebooks/{nb_id}").status_code == 404


def test_get_update_delete_unknown_notebook_404(client):
    assert client.get("/api/sandbox/notebooks/999999999").status_code == 404
    assert client.put("/api/sandbox/notebooks/999999999", json={"name": "x", "cells": []}).status_code == 404
    assert client.delete("/api/sandbox/notebooks/999999999").status_code == 404


def test_run_executes_cells_in_order_and_returns_display(client):
    res = client.post("/api/sandbox/run", json={
        "cells": [_cell("c1", "x = 21"), _cell("c2", "x * 2")],
        "run_upto": 1,
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True
    assert data["cells"][1]["display"] == {"kind": "text", "text": "42"}


def test_run_stops_on_error_and_reports_it(client):
    res = client.post("/api/sandbox/run", json={
        "cells": [_cell("c1", "1 / 0"), _cell("c2", "1")],
        "run_upto": 1,
    })
    data = res.json()
    assert data["cells"][0]["ok"] is False
    assert "ZeroDivisionError" in data["cells"][0]["error"]
    assert data["cells"][1]["ok"] is None


def test_run_can_scan_the_real_bucket(client):
    res = client.post("/api/sandbox/run", json={
        "cells": [_cell("c1", f'read("{SALES_SOURCE}").select(["order_id"]).limit(3)')],
        "run_upto": 0,
    })
    assert res.status_code == 200, res.text
    disp = res.json()["cells"][0]["display"]
    assert disp["kind"] == "table"
    assert disp["columns"][0]["name"] == "order_id"
    assert len(disp["rows"]) == 3


def test_run_empty_cells_rejected(client):
    assert client.post("/api/sandbox/run", json={"cells": [], "run_upto": 0}).status_code == 400


def test_run_out_of_range_run_upto_rejected(client):
    res = client.post("/api/sandbox/run", json={"cells": [_cell("c1", "1")], "run_upto": 5})
    assert res.status_code == 400


def test_run_timeout_is_killed(client):
    res = client.post("/api/sandbox/run", json={
        "cells": [_cell("c1", "import time; time.sleep(5)")],
        "run_upto": 0,
        "timeout_seconds": 1,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert "timeout" in data["error"]


def test_convert_detects_sources_and_rewrites_script(client):
    res = client.post("/api/sandbox/convert", json={
        "name": "my scratch pipe",
        "cells": [_cell("c1", f'df = read("{SALES_SOURCE}")'), _cell("c2", "output = df.head(5)")],
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["warnings"] == []
    assert data["sources"][0]["path"] == SALES_SOURCE
    assert "read(" not in data["yaml"]
    assert "sources[" in data["yaml"]
    assert "name: my_scratch_pipe" in data["yaml"]

    from app import pipelines
    filled = data["yaml"].replace(
        "s3://REPLACE/ME/target   # TODO: set a real target path before saving",
        "s3://cash-intel/pipeline_test/from_sandbox",
    )
    parsed = pipelines.parse_pipeline_text(filled)
    assert parsed.name == "my_scratch_pipe"


def test_convert_warns_when_no_output_assignment(client):
    res = client.post("/api/sandbox/convert", json={
        "name": "no output nb",
        "cells": [_cell("c1", f'read("{SALES_SOURCE}").head(2)')],
    })
    assert res.status_code == 200
    assert any("output" in w for w in res.json()["warnings"])


def test_audit_rows_recorded(client):
    from app.registry import registry

    created = client.post("/api/sandbox/notebooks", json={"name": "audit_probe", "cells": []})
    nb_id = created.json()["id"]
    client.put(f"/api/sandbox/notebooks/{nb_id}", json={"name": "audit_probe", "cells": []})
    client.post("/api/sandbox/run", json={"cells": [_cell("c1", "1")], "run_upto": 0})
    client.delete(f"/api/sandbox/notebooks/{nb_id}")

    events = registry.auth_store.audit_events()
    actions = {e["action"] for e in events}
    assert {"sandbox.create", "sandbox.update", "sandbox.run", "sandbox.delete"} <= actions
