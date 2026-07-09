"""Dashboards CRUD plus publishing to the portal's folder tree."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..registry import registry

router = APIRouter(tags=["dashboards"])


class DashboardIn(BaseModel):
    name: str
    items: list[dict] = []
    views: list[dict] = []   # named filter sets: [{"name", "filters": [...]}]
    active_view: int = 0


class PublishIn(BaseModel):
    dashboard_id: int
    folder: str = ""


def _norm_folder(folder: str) -> str:
    parts = [p.strip() for p in folder.replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)


@router.get("/dashboards")
def list_dashboards():
    return registry.store.list_dashboards()


@router.get("/dashboards/{dash_id}")
def get_dashboard(dash_id: int):
    dash = registry.store.get_dashboard(dash_id)
    if not dash:
        raise HTTPException(status_code=404, detail="dashboard not found")
    # resolve tiles to their visuals in one call; deleted visuals resolve to None
    dash["visuals"] = {
        str(item["visual_id"]): registry.store.get(item["visual_id"]) for item in dash["items"]
    }
    return dash


@router.post("/dashboards", status_code=201)
def create_dashboard(d: DashboardIn):
    return registry.store.create_dashboard(d.name, d.items, d.views, d.active_view)


@router.put("/dashboards/{dash_id}")
def update_dashboard(dash_id: int, d: DashboardIn):
    updated = registry.store.update_dashboard(dash_id, d.name, d.items, d.views, d.active_view)
    if not updated:
        raise HTTPException(status_code=404, detail="dashboard not found")
    return updated


@router.delete("/dashboards/{dash_id}", status_code=204)
def delete_dashboard(dash_id: int):
    if not registry.store.delete_dashboard(dash_id):
        raise HTTPException(status_code=404, detail="dashboard not found")


@router.post("/publish")
def publish(p: PublishIn):
    result = registry.store.publish(p.dashboard_id, _norm_folder(p.folder))
    if not result:
        raise HTTPException(status_code=404, detail="dashboard not found")
    return result


@router.delete("/publish/{dashboard_id}", status_code=204)
def unpublish(dashboard_id: int):
    if not registry.store.unpublish(dashboard_id):
        raise HTTPException(status_code=404, detail="not published")


@router.get("/portal")
def portal():
    return {"publications": registry.store.list_publications()}
