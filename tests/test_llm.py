"""app.llm: the tool schema/system prompt the Translator sends to the LLM.

Regression guards for the bug this module was patched for: filters[].op had
no declared vocabulary at all (the model guessed '=' instead of 'eq', which
engine.run_query then rejected with an unexplained "unsupported filter op"
error) and the grain field's only guidance was a wrong example ("1qtr" isn't
a real grain). These tests assert the schema/prompt stay derived from
engine.FILTER_OPS / semantic.TIME_GRAINS, not a second hand-written copy that
can drift out of sync with them again.
"""
from __future__ import annotations

from app import config, engine, llm
from app.semantic import TIME_GRAINS


def _tool(name):
    return next(t for t in llm._TOOLS if t["name"] == name)


def test_filter_op_enum_matches_engine_filter_ops():
    op_schema = _tool("propose_query")["input_schema"]["properties"]["filters"]["items"]["properties"]["op"]
    assert set(op_schema["enum"]) == engine.FILTER_OPS
    # the exact bug reported: '=' must never be an accepted op
    assert "=" not in op_schema["enum"]


def test_grain_enum_matches_time_grains_not_the_old_wrong_example():
    dim_schema = _tool("propose_query")["input_schema"]["properties"]["dimensions"]["items"]["oneOf"][1]
    grain_schema = dim_schema["properties"]["grain"]
    assert set(grain_schema["enum"]) == set(TIME_GRAINS)
    assert "1qtr" not in grain_schema["enum"]
    assert "1q" in grain_schema["enum"]


def test_system_prompt_declares_filter_ops_and_grains():
    for op in engine.FILTER_OPS:
        assert op in llm._SYSTEM_PROMPT
    for grain in TIME_GRAINS:
        assert grain in llm._SYSTEM_PROMPT


def test_show_last_query_tool_is_declared_with_no_required_args():
    tool = _tool("show_last_query")
    assert tool["input_schema"].get("required", []) == []


def test_all_four_tool_kinds_present():
    assert {t["name"] for t in llm._TOOLS} == {
        "propose_query", "ask_clarification", "show_last_query", "decline",
    }


def test_all_tools_have_eager_input_streaming_enabled():
    """Lets AnthropicTranslator.translate_streaming() show a tool's args
    taking shape live (input_json events), instead of only ever seeing the
    whole JSON blob appear at once at the end."""
    assert all(t.get("eager_input_streaming") is True for t in llm._TOOLS)


# ── measure formula ground truth (a name/description alone isn't always
# enough to disambiguate measures — see nlq._measure_catalog_entry) ───────

def test_catalog_text_includes_measure_formula_when_present():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="", dimensions=[],
            measures=[{"name": "revenue", "label": "Revenue", "description": "",
                       "expr": "sum(unit_price * quantity)"}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "computed as: sum(unit_price * quantity)" in text


def test_catalog_text_omits_formula_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="", dimensions=[],
            measures=[{"name": "orders", "label": "Orders", "description": ""}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "computed as" not in text


def test_system_prompt_explains_the_formula_field():
    assert "computed as" in llm._SYSTEM_PROMPT


# ── synonyms (alternate business vocabulary) ───────────────────────────────

def test_catalog_text_includes_synonyms_for_dimensions_and_measures():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "order_date", "label": "Order Date", "type": "time",
                         "description": "", "synonyms": ["date", "purchase date"]}],
            measures=[{"name": "revenue", "label": "Revenue", "description": "",
                       "synonyms": ["sales", "turnover"]}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "also called: date, purchase date" in text
    assert "also called: sales, turnover" in text


def test_catalog_text_omits_synonyms_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "category", "label": "Category", "type": "categorical",
                         "description": "", "synonyms": []}],
            measures=[{"name": "orders", "label": "Orders", "description": "", "synonyms": []}],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "also called" not in text


def test_system_prompt_explains_synonyms_and_requires_declared_name():
    assert "also called" in llm._SYSTEM_PROMPT
    assert "never a synonym string" in llm._SYSTEM_PROMPT


# ── inline measures (chat-authored running_total()/lag()) ─────────────────

def test_propose_query_tool_declares_inline_measures():
    schema = _tool("propose_query")["input_schema"]["properties"]["inline_measures"]
    item_props = schema["items"]["properties"]
    assert set(schema["items"]["required"]) == {"name", "expr"}
    assert {"name", "expr", "label", "format"} <= set(item_props)


def test_system_prompt_explains_inline_measures():
    assert "running_total" in llm._SYSTEM_PROMPT
    assert "lag(measure" in llm._SYSTEM_PROMPT
    assert "inline_measures" in llm._SYSTEM_PROMPT


# ── categorical sample values ("common sense" case/format matching) ───────

def test_catalog_text_includes_sample_values_when_present():
    catalog = [
        llm.ModelCatalogEntry(
            name="clinical_ops_recruitment", label="Recruitment Events", description="",
            dimensions=[{"name": "therapeutic_area", "label": "Therapeutic Area", "type": "categorical",
                         "description": "", "synonyms": [], "sample_values": ["Cardiology", "Oncology"]}],
            measures=[],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "sample values: Cardiology, Oncology" in text


def test_catalog_text_omits_sample_values_marker_when_absent():
    catalog = [
        llm.ModelCatalogEntry(
            name="sales", label="Sales Orders", description="",
            dimensions=[{"name": "order_date", "label": "Order Date", "type": "time",
                         "description": "", "synonyms": []}],
            measures=[],
        ),
    ]
    text = llm._catalog_text(catalog)
    assert "sample values" not in text


def test_system_prompt_explains_sample_values():
    assert "sample values" in llm._SYSTEM_PROMPT


# ── propose_query's `model` field, constrained to the live catalog (the bug
# this fixes: with no declared vocabulary at all, the LLM could omit `model`
# — most visible under a single-model scope, where nlq.py's re-validation
# declined with the confusing "'None' is not a model this conversation can
# query.") ───────────────────────────────────────────────────────────────

def test_tools_for_catalog_constrains_model_to_catalog_names():
    catalog = [
        llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[]),
        llm.ModelCatalogEntry(name="logistics", label="Logistics", description="", dimensions=[], measures=[]),
    ]
    tools = llm._tools_for_catalog(catalog)
    propose = next(t for t in tools if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {
        "type": "string", "enum": ["sales", "logistics"],
    }
    # required/other tools are untouched
    assert propose["input_schema"]["required"] == ["model", "dimensions", "measures"]
    assert {t["name"] for t in tools} == {t["name"] for t in llm._TOOLS}


def test_tools_for_catalog_leaves_model_unconstrained_when_catalog_is_empty():
    """No models available to this conversation at all — nothing to
    constrain `model` to, so it stays the plain string it always was."""
    tools = llm._tools_for_catalog([])
    assert tools is llm._TOOLS
    propose = next(t for t in tools if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string"}


def test_tools_for_catalog_does_not_mutate_the_shared_tools_list():
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]
    llm._tools_for_catalog(catalog)
    propose = next(t for t in llm._TOOLS if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string"}


# ── adaptive thinking is only sent to models that support it (the bug this
# fixes: Haiku doesn't, and got adaptive thinking unconditionally, 400ing
# with "adaptive thinking is not supported on this model") ────────────────

def test_thinking_kwargs_enabled_for_adaptive_capable_models():
    for model in ("claude-opus-4-8", "claude-sonnet-5"):
        assert llm._thinking_kwargs(model) == {"thinking": {"type": "adaptive", "display": "summarized"}}


def test_thinking_kwargs_omitted_for_haiku():
    assert llm._thinking_kwargs("claude-haiku-4-5-20251001") == {}


def test_adaptive_thinking_models_are_a_subset_of_llm_model_choices():
    """Guards against a typo drifting the two lists apart — every entry here
    must be one of the actually-selectable models (config.LLM_MODEL_CHOICES'
    own comment asks for the same discipline)."""
    assert llm._ADAPTIVE_THINKING_MODELS <= set(config.LLM_MODEL_CHOICES)


# ── corporate proxy / TLS-inspecting proxy support (CI_LLM_PROXY /
# CI_LLM_CA_BUNDLE) — a Zscaler-style proxy re-signs HTTPS with its own CA,
# so the default client (which already honors HTTP_PROXY/HTTPS_PROXY on its
# own) fails TLS verification without an explicit CA bundle to trust ──────

def test_anthropic_client_is_plain_when_no_proxy_config_set(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROXY", None)
    monkeypatch.setattr(config, "LLM_CA_BUNDLE", None)
    client = llm._anthropic_client("x")
    import anthropic
    assert type(client._client) is anthropic._base_client.SyncHttpxClientWrapper


def test_anthropic_client_uses_custom_http_client_when_proxy_set(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROXY", "http://proxy.corp:8080")
    monkeypatch.setattr(config, "LLM_CA_BUNDLE", None)
    client = llm._anthropic_client("x")
    import anthropic
    assert type(client._client) is anthropic.DefaultHttpxClient


_DUMMY_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIC/zCCAeegAwIBAgIUawi9/62MdUdVG0t7+GX+36u88+4wDQYJKoZIhvcNAQEL
BQAwDzENMAsGA1UEAwwEdGVzdDAeFw0yNjA3MjMxMDA2MzlaFw0yNjA3MjQxMDA2
MzlaMA8xDTALBgNVBAMMBHRlc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEK
AoIBAQDJ2Sxk2fs1aYN4Bvsz2t5MnUmCWRbV6tJhNHeEzbMQgcll5YbKamVNQGiP
wQop4XYsAdO0UJLPsV1gYtv8616w8amqEKxzae9N1S+Q8G1AYYjShbICshBEJ4Ch
3YGThpW2yf8relxmXYxxMHHAudx24XJgX2nDKH6nqdCSLtwN2bz8wKc+pHpjMVhW
tldHvwHxyzef2iDBJUgaQIm6rbXtUjIDJMe+7Gn/czdOsna7dwCZqOjLWbM/3+2q
Zzu3THopagEhR4yM98b4nOT3TWV7Ruk2F2qp+55R2mPM83zn5EHGRg+nXulmDW5v
nLGLFfBG3julei5vxNdA4xwVAp+FAgMBAAGjUzBRMB0GA1UdDgQWBBQzMsYgKjwy
r0TAjnB72bXOhnCPBzAfBgNVHSMEGDAWgBQzMsYgKjwyr0TAjnB72bXOhnCPBzAP
BgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAxTBbXpWgD27lAYIZ9
uHKoZth1tF0g2HKXuyce974A0Y51L+cT9g7lqZSAXAWZ+AHzrpO6M0THl92qLC1M
DJrI9FQr4efNczRdfKYJd9oyqCIAi2OmJX7PFCbCSITwo6651kzC2c99wtyqwi3v
JXsLlBZtep5Fy5Krq6eFgvOAHbfrVL9+QWjRhaH+B4F9ErlqyhJUAApXanGDQ82L
4V6PaPbcKwQczecD918seDvzjqFkcDD86viJ4NZf9D4GbutR90QuRGF1OAacButz
+kVD3PITDliq906O6MXqcC+BQvCGnjsCSljXXHfFsymhFee4I2OYdgCE9NwEp/SA
15iN
-----END CERTIFICATE-----
"""


def test_anthropic_client_uses_custom_http_client_when_ca_bundle_set(monkeypatch, tmp_path):
    ca_bundle = tmp_path / "zscaler-root.pem"
    ca_bundle.write_text(_DUMMY_CA_PEM)
    monkeypatch.setattr(config, "LLM_PROXY", None)
    monkeypatch.setattr(config, "LLM_CA_BUNDLE", str(ca_bundle))
    client = llm._anthropic_client("x")
    import anthropic
    assert type(client._client) is anthropic.DefaultHttpxClient


def test_translate_streaming_wires_thinking_and_model_enum_into_the_real_call(monkeypatch):
    """Integration-level guard, not just the pure helpers in isolation: proves
    AnthropicTranslator.translate_streaming() actually passes _thinking_kwargs()
    and _tools_for_catalog() through to messages.stream() — thinking omitted
    for a non-adaptive model (haiku; the exact reported 400), and
    propose_query's `model` constrained to the catalog's own names."""
    import anthropic

    captured = {}

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(())

        def get_final_message(self):
            block = type("Block", (), {"type": "tool_use", "name": "decline", "input": {"reason_text": "x"}})()
            return type("Message", (), {"content": [block]})()

    class FakeMessages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    catalog = [llm.ModelCatalogEntry(name="sales", label="Sales", description="", dimensions=[], measures=[])]

    haiku = llm.AnthropicTranslator(api_key="x", model="claude-haiku-4-5-20251001")
    list(haiku.translate_streaming("q", catalog, []))
    assert "thinking" not in captured
    propose = next(t for t in captured["tools"] if t["name"] == "propose_query")
    assert propose["input_schema"]["properties"]["model"] == {"type": "string", "enum": ["sales"]}

    captured.clear()
    sonnet = llm.AnthropicTranslator(api_key="x", model="claude-sonnet-5")
    list(sonnet.translate_streaming("q", catalog, []))
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
