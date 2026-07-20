"""Notebooks CRUD + the first-run seeded "Recruitment Overview" sample."""


def test_notebooks_roundtrip(author_client):
    created = author_client.post("/api/notebooks", json={"name": "n", "html": "<p>hi</p>"}).json()
    assert created["html"] == "<p>hi</p>"
    assert any(n["id"] == created["id"] for n in author_client.get("/api/notebooks").json())
    fetched = author_client.get(f"/api/notebooks/{created['id']}").json()
    assert fetched["name"] == "n"

    updated = author_client.put(f"/api/notebooks/{created['id']}",
                                 json={"name": "n2", "html": "<p>bye</p>"}).json()
    assert updated["name"] == "n2" and updated["html"] == "<p>bye</p>"

    assert author_client.delete(f"/api/notebooks/{created['id']}").status_code == 204
    assert author_client.get(f"/api/notebooks/{created['id']}").status_code == 404


def test_notebook_requires_author_role(viewer_client):
    assert viewer_client.post("/api/notebooks", json={"name": "n", "html": ""}).status_code == 403


def test_notebook_not_found_is_404(client):
    assert client.get("/api/notebooks/999999").status_code == 404


def test_seeded_recruitment_notebook_present(client):
    notebooks = client.get("/api/notebooks").json()
    demo = next((n for n in notebooks if n["name"] == "Recruitment Overview"), None)
    assert demo is not None

    fetched = client.get(f"/api/notebooks/{demo['id']}").json()
    html = fetched["html"]
    assert "nb-tabs" in html and "nb-collapsible" in html
    assert "nb-visual" in html and "nb-dashboard" in html

    # every embedded visual/dashboard id in the html actually resolves
    import re
    visuals = {v["id"] for v in client.get("/api/visuals").json()}
    for vid in re.findall(r'class="nb-visual" data-visual-id="(\d+)"', html):
        assert int(vid) in visuals
    for did in re.findall(r'data-dashboard-id="(\d+)"', html):
        assert client.get(f"/api/dashboards/{did}").status_code == 200
