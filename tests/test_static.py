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
