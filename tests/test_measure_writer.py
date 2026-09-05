"""AI-authored measures: the context the model is shown, the re-validation
every proposal passes through, the propose -> verify -> repair loop, and the
/api/measures/write/stream surface.

The real LLMMeasureWriter is swapped for a scripted FakeWriter (no network
calls) — but verification is NOT faked: every check() and verify() below runs
the real sqlgrammar/semantic/engine path against the real seeded bucket, which
is the whole point of the seam. A measure that passes here is one that
actually ran.
"""
from __future__ import annotations

import pytest

from app import config, llmclient
from app import measurewriter as mw
from app.api import measures as measures_api

from .test_chat_api import _parse_sse


# ── a scripted writer ───────────────────────────────────────────────────────

class FakeWriter:
    """Yields one scripted proposal per call, in order, recording the request
    it was given — which is how the repair turns are asserted: attempt 2's
    request has to carry attempt 1's rejection."""

    def __init__(self, proposals, error: str | None = None, thinking: str = ""):
        self.proposals = list(proposals)
        self.error = error
        self.thinking = thinking
        self.requests: list[mw.WriteRequest] = []

    def write_streaming(self, request):
        self.requests.append(request)
        if self.error:
            raise mw.WriterError(self.error)
        proposal = self.proposals[min(len(self.requests) - 1, len(self.proposals) - 1)]
        if self.thinking:
            yield mw.WriteStreamEvent(kind="thinking", text=self.thinking)
        if isinstance(proposal, mw.RawMeasure):
            yield mw.WriteStreamEvent(kind="draft", draft={"name": proposal.name})
        yield mw.WriteStreamEvent(kind="done", final=proposal)


def measure(**kwargs) -> mw.RawMeasure:
    kwargs.setdefault("rationale", "because")
    return mw.RawMeasure(**kwargs)


@pytest.fixture(scope="module")
def sales(models):
    return models["sales"]


@pytest.fixture(scope="module")
def sales_ctx(sales):
    return mw.build_context(sales, scope="model")


@pytest.fixture(scope="module")
def subs(models):
    """The model with the shapes complex measures are actually asked for:
    one row per subscription, a start and an end per customer."""
    return models["subscriptions"]


@pytest.fixture(scope="module")
def subs_ctx(subs):
    return mw.build_context(subs, scope="model")


# ── the context: introspected, never taken from the caller ──────────────────

def test_context_carries_real_columns_dimensions_and_formulas(sales_ctx):
    columns = {c["name"] for c in sales_ctx.columns}
    assert {"unit_price", "quantity", "order_id", "order_date"} <= columns
    assert all(c["dtype"] for c in sales_ctx.columns)

    channel = next(d for d in sales_ctx.dimensions if d["name"] == "channel")
    assert "web" in channel["sample_values"]      # real stored values, for FILTER predicates

    revenue = next(m for m in sales_ctx.measures if m["name"] == "revenue")
    assert revenue["expr"] == "SUM(unit_price * quantity)"
    assert revenue["format"] == "currency"


def test_context_carries_the_visuals_query_and_its_inline_measures(sales):
    query = {
        "dimensions": [{"name": "order_date", "grain": "1mo"}],
        "measures": ["revenue"],
        "inline_measures": [{"name": "running_revenue", "expr": "SUM(revenue) OVER w"}],
        "parameters": [{"name": "periods", "type": "int", "values": [1, 4], "default": 1}],
    }
    ctx = mw.build_context(sales, scope="visual", query=query)
    assert ctx.scope == "visual"
    assert [p["name"] for p in ctx.parameters] == ["periods"]
    inline = next(m for m in ctx.measures if m["name"] == "running_revenue")
    assert inline["inline"] is True
    assert "running_revenue" in ctx.taken_names


def test_context_survives_an_unreachable_source(sales, monkeypatch):
    monkeypatch.setattr(mw.engine, "scan_schema",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bucket gone")))
    ctx = mw.build_context(sales, scope="model")
    assert "bucket gone" in ctx.schema_error
    assert ctx.columns == []


def test_target_part_needs_a_named_fact_table_on_a_composite_model(models):
    composite = models["commercial_overview"]
    with pytest.raises(ValueError, match="unrelated fact tables"):
        mw.target_part(composite)
    part = mw.target_part(composite, "orders")
    assert "revenue" in part.measures


# ── the prompt: everything the writer needs, nothing invented ───────────────

def test_prompt_shows_columns_formulas_and_the_from_contract(sales_ctx):
    prompt = mw.build_user_prompt(mw.WriteRequest(instruction="revenue per order", context=sales_ctx))
    assert "unit_price" in prompt and "order_id" in prompt
    assert "SUM(unit_price * quantity)" in prompt          # house style to match
    assert "INTO THE MODEL" in prompt and "param()" in prompt
    system = mw._system_prompt()
    assert "{model}" in system and "{dims}" in system and "emits:" in system
    assert "aggregate of an aggregate" in system
    # the function allowlist is read from the same catalog the validator uses
    assert "median" in system


def test_prompt_feeds_every_rejection_back_for_repair(sales_ctx):
    rejected = measure(name="aov", expr="AVG(order_total)",
                       from_source="SELECT order_id FROM {model} GROUP BY order_id")
    req = mw.WriteRequest(instruction="average order value", context=sales_ctx,
                          attempts=[mw.Attempt(measure=rejected, error="lost dimension column(s) ['channel']")])
    prompt = mw.build_user_prompt(req)
    assert "REJECTED: lost dimension column(s) ['channel']" in prompt
    assert "do not resend the same expression" in prompt


def test_visual_prompt_shows_the_query_and_its_parameters(sales):
    ctx = mw.build_context(sales, scope="visual", query={
        "dimensions": [{"name": "order_date", "grain": "1q"}], "measures": ["revenue"],
        "parameters": [{"name": "periods", "type": "int", "values": [1, 4], "default": 4}],
    })
    prompt = mw.build_user_prompt(mw.WriteRequest(instruction="growth", context=ctx))
    assert "grain 1q" in prompt and "ON A VISUAL" in prompt
    assert "param('name')" in prompt and "periods (int" in prompt


# ── the request: enough budget to think AND to write ────────────────────────

def test_the_request_budgets_the_answer_and_caches_the_system_prompt(sales_ctx, monkeypatch):
    """What ran out on the first complex measure anyone asked for: 4096 tokens
    covering both the reasoning "the median time to a customer's first order,
    by signup cohort" needs and the from: block it then has to write. The seam
    now asks for the answer alone and app/llmclient.py adds the room to think
    on top of it."""
    monkeypatch.setattr(config, "LLM_REASONING_TOKENS", 8192)
    writer = mw.LLMMeasureWriter(api_key="k", model="claude-sonnet-5", client=object())

    request = writer._request(mw.WriteRequest(
        instruction="median days to a customer's first order, by signup cohort",
        context=sales_ctx))
    assert request.max_tokens == config.MEASURE_WRITER_MAX_TOKENS
    # the shipped default has to leave room for a from: block (up to
    # sqlgrammar.MAX_RELATION_LEN characters) once the thinking is paid for
    assert config.MEASURE_WRITER_MAX_TOKENS >= 8192
    assert request.thinking
    # long, static, and re-sent on every repair round of every turn
    assert request.cache_system

    assert llmclient.AnthropicClient()._kwargs(request)["max_tokens"] == (
        config.MEASURE_WRITER_MAX_TOKENS + 8192)


def test_the_system_prompt_is_byte_identical_between_attempts():
    """A cache breakpoint is only worth declaring if the prefix under it does
    not move — and the aggregate list is read from a live catalog, which is
    exactly the kind of thing that would move."""
    assert mw._system_prompt() == mw._system_prompt()


# ── check(): the same rules the save paths enforce ──────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (measure(name="Bad Name", expr="SUM(quantity)"), "snake_case"),
    (measure(name="revenue", expr="SUM(quantity)"), "already the name"),
    (measure(name="x", expr=""), "no expression"),
    (measure(name="x", expr="SUM(quantity)", format="dollars"), "format 'dollars'"),
    (measure(name="x", expr="SUM(nope)"), "nope"),
    (measure(name="x", expr="quantity"), "aggregate"),
    (measure(name="x", expr="SUM(quantity)", emits=["channel"]), "'emits' only means something"),
    (measure(name="x", expr="COUNT(*)", from_source="SELECT {dims} FROM {model}",
             emits=["not_a_dim"]), "not declared dimensions"),
    (measure(name="x", expr="COUNT(*)",
             from_source="SELECT * FROM read_parquet('s3://other/x.parquet')"), "table function"),
])
def test_check_rejects(sales_ctx, raw, expected):
    error = mw.check(sales_ctx, raw)
    assert error and expected in error


def test_check_accepts_the_three_shapes(sales_ctx):
    assert mw.check(sales_ctx, measure(name="ai_units", expr="SUM(quantity)")) is None
    assert mw.check(sales_ctx, measure(name="running_revenue", expr="SUM(revenue) OVER w")) is None
    assert mw.check(sales_ctx, measure(
        name="ai_aov", expr="AVG(order_total)",
        from_source="SELECT {dims}, SUM(unit_price * quantity) AS order_total\n"
                    "FROM {model} GROUP BY {dims}, order_id")) is None


def test_check_rejects_a_window_over_something_that_is_not_a_measure(sales_ctx):
    # inside a window expression a bare name is a sibling measure, so a raw
    # column is exactly the mistake to catch
    assert "unit_price" in (mw.check(sales_ctx, measure(name="x", expr="SUM(unit_price) OVER w")) or "")


def test_parameters_are_visual_scoped_only(sales):
    parameterized = measure(name="prior", expr="LAG(revenue, param('periods')) OVER w")
    model_ctx = mw.build_context(sales, scope="model")
    assert "cannot be saved" in mw.check(model_ctx, parameterized)

    visual_ctx = mw.build_context(sales, scope="visual", query={
        "parameters": [{"name": "periods", "type": "int", "values": [1, 4], "default": 1}]})
    assert mw.check(visual_ctx, parameterized) is None

    undeclared = mw.build_context(sales, scope="visual", query={})
    assert "has not declared" in mw.check(undeclared, parameterized)

    wrong_type = mw.build_context(sales, scope="visual", query={
        "parameters": [{"name": "periods", "type": "float", "values": [1.0], "default": 1.0}]})
    assert "must be int" in mw.check(wrong_type, parameterized)


# ── verify(): it actually runs ──────────────────────────────────────────────

def test_verify_runs_a_plain_measure_and_previews_it(sales, sales_ctx):
    verdict = mw.verify(sales, sales_ctx, measure(name="ai_units", expr="SUM(quantity)"))
    assert verdict.ok and verdict.ran
    assert verdict.preview["value"] > 0


def test_verify_runs_a_complex_measure_end_to_end(sales, sales_ctx):
    verdict = mw.verify(sales, sales_ctx, measure(
        name="avg_order_value", expr="AVG(order_total)", format="currency",
        from_source="SELECT {dims}, SUM(unit_price * quantity) AS order_total\n"
                    "FROM {model}\nGROUP BY {dims}, order_id"))
    assert verdict.ok and verdict.ran, verdict.error
    assert verdict.preview["value"] > 0


def test_verify_catches_a_from_block_that_drops_the_query_dimensions(sales, sales_ctx):
    """The most common way a complex measure breaks — and unfindable without
    running it, since the block compiles perfectly well on its own."""
    verdict = mw.verify(sales, sales_ctx, measure(
        name="avg_order_value", expr="AVG(order_total)",
        from_source="SELECT SUM(unit_price * quantity) AS order_total\n"
                    "FROM {model}\nGROUP BY order_id"))
    assert not verdict.ok
    assert "lost dimension column" in verdict.error


def test_verify_catches_an_emits_the_block_never_outputs(sales, sales_ctx):
    verdict = mw.verify(sales, sales_ctx, measure(
        name="x", expr="COUNT(*)", emits=["order_date"],
        from_source="SELECT {dims}, order_id FROM {model} GROUP BY {dims}, order_id"))
    assert not verdict.ok
    assert "does not output" in verdict.error


def test_verify_runs_a_window_measure_by_supplying_a_time_dimension(sales, sales_ctx):
    verdict = mw.verify(sales, sales_ctx, measure(name="running_revenue", expr="SUM(revenue) OVER w"))
    assert verdict.ok and verdict.ran, verdict.error


def test_verify_reports_an_unreachable_source_instead_of_blaming_the_measure(sales, monkeypatch):
    ctx = mw.build_context(sales, scope="model")
    object.__setattr__(ctx, "schema_error", "source not reachable: boom")
    verdict = mw.verify(sales, ctx, measure(name="ai_units", expr="SUM(quantity)"))
    assert verdict.ok and not verdict.ran
    assert "boom" in verdict.note


# ── the worked patterns the prompt teaches ──────────────────────────────────
# The system prompt hands the model three from: shapes to copy and one join
# rule. Every one of them is asserted here the only way that means anything:
# by running it against the real bucket. A pattern that stopped working would
# otherwise go on being taught, and be found by an author's rejected measure.

def test_the_prompt_teaches_the_placeholder_rules_and_a_preflight():
    system = mw._system_prompt()
    assert "USING ({dims})" in system                   # the ambiguous-join fix
    assert "qualifies only the first name" in system    # f.{dims} does not work
    assert "BEFORE YOU ANSWER" in system                # the four-point re-read
    assert "WORKED PATTERNS" in system


def test_pattern_a_time_to_an_entitys_first_event_runs(subs, subs_ctx):
    """The prompt's hardest pattern, and the one an author reaches for first:
    a per-entity interval, reduced to a median, bucketed by a date the block
    derives rather than one the raw rows carry."""
    verdict = mw.verify(subs, subs_ctx, measure(
        name="median_days_to_churn", expr="MEDIAN(days_to_first)", emits=["start_month"],
        from_source="SELECT {dims},\n"
                    "       date_diff('day', MIN(start_date), MIN(end_date)) AS days_to_first,\n"
                    "       MIN(start_date) AS start_month\n"
                    "FROM {model}\n"
                    "WHERE end_date IS NOT NULL\n"
                    "GROUP BY {dims}, customer_id"))
    assert verdict.ok and verdict.ran, verdict.error
    assert verdict.preview["value"] > 0


def test_pattern_c_semi_additive_takes_each_entitys_latest_row(subs, subs_ctx):
    """QUALIFY with the query's dimensions inside the partition — the shape a
    balance or level needs, and the one that silently double-counts if the
    dimensions are left out of it."""
    verdict = mw.verify(subs, subs_ctx, measure(
        name="latest_fee", expr="SUM(monthly_fee)", format="currency",
        from_source="SELECT {dims}, monthly_fee\n"
                    "FROM {model}\n"
                    "QUALIFY row_number() OVER (PARTITION BY {dims}, customer_id "
                    "ORDER BY start_date DESC) = 1"))
    assert verdict.ok and verdict.ran, verdict.error
    assert verdict.preview["value"] > 0


def test_two_ctes_carrying_the_dimensions_join_with_using(subs, subs_ctx):
    """{dims} expands to bare quoted names, so both sides of a self-join carry
    the same column names and the final SELECT {dims} is ambiguous. USING
    ({dims}) is the fix the prompt gives — and the error it avoids is asserted
    here too, because that is what makes the rule worth teaching."""
    block = ("WITH per_customer AS (\n"
             "  SELECT {dims}, customer_id, SUM(monthly_fee) AS fee\n"
             "  FROM {model} GROUP BY {dims}, customer_id\n"
             "), totals AS (\n"
             "  SELECT {dims}, SUM(monthly_fee) AS total FROM {model} GROUP BY {dims}\n"
             ")\n"
             "SELECT {dims}, fee / NULLIF(total, 0) AS share\n"
             "FROM per_customer JOIN totals %s")
    ok = mw.verify(subs, subs_ctx, measure(
        name="top_customer_share", expr="MAX(share)", format="percent",
        from_source=block % "USING ({dims})"))
    assert ok.ok and ok.ran, ok.error

    ambiguous = mw.verify(subs, subs_ctx, measure(
        name="top_customer_share", expr="MAX(share)", format="percent",
        from_source=block % "ON TRUE"))
    assert not ambiguous.ok
    assert "Ambiguous reference" in ambiguous.error


# ── the loop: propose -> verify -> repair ───────────────────────────────────

def test_a_rejected_proposal_is_repaired_with_the_error_in_hand(sales, sales_ctx):
    broken = measure(name="avg_order_value", expr="AVG(order_total)",
                     from_source="SELECT SUM(unit_price * quantity) AS order_total\n"
                                 "FROM {model} GROUP BY order_id")
    fixed = measure(name="avg_order_value", expr="AVG(order_total)", format="currency",
                    from_source="SELECT {dims}, SUM(unit_price * quantity) AS order_total\n"
                                "FROM {model} GROUP BY {dims}, order_id")
    writer = FakeWriter([broken, fixed])
    outcome = mw.run(writer, sales, mw.WriteRequest(instruction="average order value", context=sales_ctx))

    assert outcome.status == "written"
    assert outcome.measure.from_source == fixed.from_source
    assert outcome.verdict.ran
    assert len(outcome.attempts) == 1                       # the repair is reported, not hidden
    assert "lost dimension column" in outcome.attempts[0].error
    # the second call is what makes this a repair rather than a retry
    assert writer.requests[0].attempts == []
    assert "lost dimension column" in writer.requests[1].attempts[0].error


def test_the_loop_gives_up_with_the_last_error_rather_than_a_broken_measure(sales, sales_ctx):
    broken = measure(name="x", expr="SUM(no_such_column)")
    outcome = mw.run(FakeWriter([broken]), sales,
                     mw.WriteRequest(instruction="nonsense", context=sales_ctx))
    assert outcome.status == "failed"
    assert "no_such_column" in outcome.error
    assert len(outcome.attempts) == mw.MAX_ATTEMPTS
    assert outcome.measure is not None      # what was tried is still shown to the author


def test_a_decline_ends_the_turn_immediately(sales, sales_ctx):
    writer = FakeWriter([mw.RawDecline(reason="sales has no cost of capital column")])
    outcome = mw.run(writer, sales, mw.WriteRequest(instruction="wacc", context=sales_ctx))
    assert outcome.status == "declined"
    assert "cost of capital" in outcome.reason
    assert len(writer.requests) == 1


def test_a_verified_measure_is_saveable_through_the_ordinary_endpoint(sales, sales_ctx, admin_client):
    """The seam's contract: what it verifies, the author-gated save path
    accepts — no second, more permissive door into the model."""
    outcome = mw.run(FakeWriter([measure(name="ai_units", expr="SUM(quantity)", label="Units")]),
                     sales, mw.WriteRequest(instruction="total units", context=sales_ctx))
    assert outcome.status == "written"
    body = outcome.measure.to_dict()
    res = admin_client.post("/api/models/sales/measures", json=body)
    try:
        assert res.status_code == 201, res.text
    finally:
        admin_client.delete(f"/api/models/sales/measures/{body['name']}")


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def llm_enabled(monkeypatch):
    monkeypatch.setattr(measures_api.config, "LLM_ENABLED", True)


def _fake(monkeypatch, proposals, **kwargs) -> FakeWriter:
    writer = FakeWriter(proposals, **kwargs)
    monkeypatch.setattr(measures_api, "_writer", writer)
    return writer


def _write(client, **body):
    body.setdefault("instruction", "total units sold")
    body.setdefault("model", "sales")
    return client.post("/api/measures/write/stream", json=body)


def test_route_streams_a_verified_measure(author_client, monkeypatch):
    _fake(monkeypatch, [measure(name="ai_total_units", expr="SUM(quantity)", label="Units",
                                description="Units sold.", rationale="plain sum")],
          thinking="considering")
    res = _write(author_client)
    assert res.status_code == 200
    events = _parse_sse(res.text)
    kinds = [name for name, _ in events]
    assert "thinking" in kinds and "draft" in kinds and "verifying" in kinds
    name, payload = events[-1]
    assert name == "response" and payload["outcome"] == "written"
    assert payload["measure"]["expr"] == "SUM(quantity)"
    assert payload["verified"] is True
    assert payload["preview"]["value"] > 0
    assert payload["rationale"] == "plain sum"


def test_route_carries_the_thinking_toggle_through_to_the_writer(author_client, monkeypatch):
    """The ASK AI bar's THINKING toggle is only worth having if what it says
    reaches the request. An absent field still means "the server's default",
    which is what an untouched bar sends."""
    writer = _fake(monkeypatch, [measure(name="ai_units", expr="SUM(quantity)")])
    assert _write(author_client, thinking=False).status_code == 200
    assert writer.requests[-1].thinking is False

    assert _write(author_client, thinking=True).status_code == 200
    assert writer.requests[-1].thinking is True

    assert _write(author_client).status_code == 200
    assert writer.requests[-1].thinking is None


def test_route_reports_the_repair_round(author_client, monkeypatch):
    _fake(monkeypatch, [measure(name="ai_aov", expr="AVG(nope)"),
                        measure(name="ai_aov", expr="AVG(unit_price)")])
    events = _parse_sse(_write(author_client, instruction="average unit price").text)
    assert any(name == "rejected" for name, _ in events)
    payload = events[-1][1]
    assert payload["outcome"] == "written"
    assert len(payload["attempts"]) == 1 and "nope" in payload["attempts"][0]["error"]


def test_route_reports_a_decline_and_a_failure_without_a_measure(author_client, monkeypatch):
    _fake(monkeypatch, [mw.RawDecline(reason="no such column")])
    payload = _parse_sse(_write(author_client).text)[-1][1]
    assert payload["outcome"] == "declined" and "measure" not in payload

    _fake(monkeypatch, [measure(name="x", expr="SUM(nope)")])
    payload = _parse_sse(_write(author_client).text)[-1][1]
    assert payload["outcome"] == "failed" and "nope" in payload["message"]
    assert payload["rejected"]["expr"] == "SUM(nope)"


def test_route_reports_a_provider_outage_as_an_error_event(author_client, monkeypatch):
    _fake(monkeypatch, [], error="502 from the gateway")
    payload = _parse_sse(_write(author_client).text)[-1][1]
    assert payload["outcome"] == "error" and "temporarily unavailable" in payload["message"]


def test_route_writes_against_an_unsaved_draft_spec(author_client, monkeypatch):
    _fake(monkeypatch, [measure(name="ai_lines", expr="COUNT(*)")])
    spec = {
        "name": "draft_sales", "label": "Draft", "description": "",
        "datasets": [{
            "name": "draft_sales",
            "source": {"path": "s3://cash-intel/sales/*.parquet", "format": "parquet"},
            "dimensions": [{"name": "channel"}],
            "measures": [{"name": "lines", "expr": "COUNT(*)"}],
            "joins": [],
        }],
        "imports": [],
    }
    res = author_client.post("/api/measures/write/stream",
                             json={"instruction": "count the order lines", "spec": spec})
    assert res.status_code == 200
    payload = _parse_sse(res.text)[-1][1]
    assert payload["outcome"] == "written", payload
    assert "draft_sales" not in [m for m in __import__("app").registry.registry.models]


def test_route_authorization_and_configuration(viewer_client, author_client, anon_client, monkeypatch):
    assert _write(anon_client).status_code in (401, 403)
    assert _write(viewer_client).status_code == 403

    monkeypatch.setattr(measures_api.config, "LLM_ENABLED", False)
    assert _write(author_client).status_code == 503
    # role is checked before configuration: an unauthorized caller never learns
    # whether this deployment has an LLM key
    assert _write(viewer_client).status_code == 403


def test_route_rejects_a_request_it_cannot_ground(author_client, monkeypatch):
    _fake(monkeypatch, [measure(name="x", expr="SUM(quantity)")])
    assert _write(author_client, instruction="  ").status_code == 400
    assert _write(author_client, scope="galaxy").status_code == 400
    assert _write(author_client, model="nope").status_code == 404
    # a composite model has to say which fact table the measure belongs to
    res = _write(author_client, model="commercial_overview")
    assert res.status_code == 400 and "fact table" in res.json()["detail"]
    assert _write(author_client, model="commercial_overview", dataset="orders").status_code == 200


def test_route_audits_the_outcome(author_client, monkeypatch):
    from app.registry import registry

    _fake(monkeypatch, [measure(name="ai_audited", expr="SUM(quantity)")])
    _write(author_client, instruction="units please")
    entries = [e for e in registry.auth_store.audit_events() if e["action"] == "measure_write"]
    assert entries, "the turn was not audited"
    last = entries[-1]["target"]
    assert "outcome:written" in last and "measure:ai_audited" in last
    assert "model:sales" in last and "ask:'units please'" in last
