"""SQLite store: visuals, dashboards (incl. legacy migration), publications."""
import json
import sqlite3

import pytest

from app.store import VisualStore


@pytest.fixture()
def store(tmp_path):
    return VisualStore(tmp_path / "t.db")


def test_visual_crud(store):
    v = store.create("v1", "sales", {"query": {}, "chartType": "bar"})
    assert v["id"] and v["spec"]["chartType"] == "bar"
    updated = store.update(v["id"], "v2", "sales", {"query": {}, "chartType": "line"})
    assert updated["name"] == "v2"
    assert len(store.list()) == 1
    assert store.delete(v["id"])
    assert store.get(v["id"]) is None


def test_dashboard_roundtrip(store):
    d = store.create_dashboard("ops", [{"visual_id": 1, "w": 2}],
                               [{"name": "default", "filters": []}], 0)
    assert d["items"][0]["w"] == 2
    assert d["views"][0]["name"] == "default"
    d2 = store.update_dashboard(d["id"], "ops", d["items"],
                                d["views"] + [{"name": "west", "filters": [{"field": "region", "op": "eq", "value": "x"}]}], 1)
    assert len(d2["views"]) == 2 and d2["active_view"] == 1


def test_legacy_dashboard_row_migrates(store):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO dashboards (name, items, created_at, updated_at) VALUES (?, ?, '', '')",
            ("old", json.dumps([{"visual_id": 7, "w": 1}])),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    d = store.get_dashboard(row_id)
    assert d["items"] == [{"visual_id": 7, "w": 1}]
    assert d["views"] == [{"name": "default", "filters": []}]
    assert d["active_view"] == 0


def test_publications(store):
    d = store.create_dashboard("p", [], [], 0)
    assert store.publish(d["id"], "a/b")["folder"] == "a/b"
    assert store.publish(d["id"], "c")["folder"] == "c"  # republish moves it
    pubs = store.list_publications()
    assert len(pubs) == 1 and pubs[0]["folder"] == "c" and pubs[0]["name"] == "p"
    assert store.unpublish(d["id"])
    assert not store.unpublish(d["id"])
    assert store.publish(999, "x") is None  # unknown dashboard


def test_deleting_dashboard_unpublishes(store):
    d = store.create_dashboard("gone", [], [], 0)
    store.publish(d["id"], "f")
    store.delete_dashboard(d["id"])
    assert store.list_publications() == []
