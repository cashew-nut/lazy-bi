"""The query endpoint: semantic query -> polars lazy scan -> aggregated rows."""
from __future__ import annotations

import base64
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from .. import engine, extract, semantic
from .deps import get_model

router = APIRouter(tags=["query"])

ARROW_IPC_STREAM = "application/vnd.apache.arrow.stream"
# extract metadata rides on a header rather than in the body so the body stays
# a plain Arrow IPC stream Perspective can ingest with no framing of our own.
# base64 because labels are UTF-8 and header values are not.
META_HEADER = "X-Extract-Meta"


class QueryRequest(BaseModel):
    model: str
    dimensions: list = []
    measures: list[str] = []
    inline_measures: list[dict] = []   # ad-hoc exprs scoped to this query
    filters: list[dict] = []
    sort: Optional[dict] = None
    limit: Optional[int] = None
    parameters: list[dict] = []          # visual-declared: [{name, type?, values, default}]
    parameter_values: dict[str, int | float | str] = {}  # caller's current picks; missing name -> default


class ExtractRequest(QueryRequest):
    """A /query request, plus the two things an extract needs to know that an
    ordinary query doesn't. Everything else is the identical shape, resolved
    through the identical model/auth/engine path.

    `cross_dimensions` are the dimensions other tiles on the same dashboard
    display, each at the grain that tile shows it at, so a cross-filter from
    there can be applied locally (FR-006). Plain strings are accepted too, for
    a dimension with no grain.

    `interactive_filters` names the filter fields whose *values* may change
    without the tile being rebuilt — the dashboard view's filters. Those are
    kept out of the pushdown and carried as columns instead, so changing one
    costs no round trip either.
    """
    cross_dimensions: list[str | dict] = []
    interactive_filters: list[str] = []


@router.post("/query")
def run_query(req: QueryRequest):
    model = get_model(req.model)  # outside the try: unknown model stays a 404
    try:
        return engine.run_query(model, req.model_dump())
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # polars errors from bad config surface as 400s
        raise HTTPException(status_code=400, detail=f"query failed: {exc}")


@router.post("/query/extract", responses={200: {"content": {ARROW_IPC_STREAM: {}}}})
def run_extract(req: ExtractRequest):
    """The same query as POST /query, answered as an Arrow IPC extract a
    dashboard tile can re-aggregate locally (specs/016-instant-cross-filter/).

    Two shapes come back with a 200, and the caller tells them apart by
    content type: the Arrow stream, or a small JSON `{"fallback": ...}` saying
    this tile runs live instead. Declining is a *routine* answer here, not an
    error — a dashboard mixing instant and live tiles is the normal case
    (US2), so it must not colour a console red on every load. Only a genuinely
    bad request keeps an error status: 404 for an unknown model, 400 for a
    query that wouldn't have worked on /query either.
    """
    model = get_model(req.model)  # outside the try: unknown model stays a 404
    body = req.model_dump()
    cross = body.pop("cross_dimensions", [])
    interactive = body.pop("interactive_filters", [])
    try:
        payload, meta = extract.build(model, body, cross, interactive)
    except extract.NotInstantable as exc:
        return {"fallback": {"reason": str(exc), "cap": None}}
    except extract.CapExceeded as exc:
        return {"fallback": {"reason": str(exc), "cap": exc.cap,
                             "rows": exc.rows, "bytes": exc.byte_size}}
    except (semantic.ModelError, engine.QueryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # polars errors from bad config surface as 400s
        raise HTTPException(status_code=400, detail=f"query failed: {exc}")
    return Response(
        content=payload,
        media_type=ARROW_IPC_STREAM,
        headers={META_HEADER: base64.b64encode(json.dumps(meta).encode()).decode()},
    )
