"""The query endpoint: semantic query -> polars lazy scan -> aggregated rows."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import engine, semantic
from .deps import get_model

router = APIRouter(tags=["query"])


class QueryRequest(BaseModel):
    model: str
    dimensions: list = []
    measures: list[str] = []
    inline_measures: list[dict] = []   # ad-hoc exprs scoped to this query
    filters: list[dict] = []
    sort: Optional[dict] = None
    limit: Optional[int] = None


@router.post("/query")
def run_query(req: QueryRequest):
    model = get_model(req.model)  # outside the try: unknown model stays a 404
    try:
        return engine.run_query(model, req.model_dump())
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # polars errors from bad config surface as 400s
        raise HTTPException(status_code=400, detail=f"query failed: {exc}")
