"""app.llmclient: the provider-agnostic transport.

The claim this module makes is "point it at a URL and a key and it works", so
these tests pin the three things that claim rests on: URL -> provider
detection, the Anthropic-shaped request surviving translation to the OpenAI
wire intact, and the OpenAI wire's raw JSON argument fragments being
reassembled into the same live partial dicts the Anthropic SDK hands us for
free (which is what the Composer's streaming HTML and the sandbox agent's
live cells are built on).
"""
from __future__ import annotations

import json

import pytest

from app import config, llmclient


# ── provider detection (URL in, wire format out) ──────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("", "anthropic"),
    ("https://api.anthropic.com", "anthropic"),
    ("https://api.anthropic.com/v1", "anthropic"),
    # Azure OpenAI, both the v1 surface and the dated deployment surface
    ("https://acme.openai.azure.com/openai/v1/", "azure"),
    ("https://acme.openai.azure.com/openai/deployments/gpt-4o", "azure"),
    ("https://acme.cognitiveservices.azure.com/openai/v1/", "azure"),
    ("https://acme.azure-api.net/openai/v1", "azure"),
    # Bedrock: the OpenAI-compatible surface is URL+key, the native one signs
    ("https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "openai"),
    ("https://bedrock-runtime.us-east-1.amazonaws.com", "bedrock"),
    # everything else is assumed OpenAI-shaped — the common gateway dialect
    ("https://openrouter.ai/api/v1", "openai"),
    ("http://localhost:11434/v1", "openai"),
    ("https://api.openai.com/v1", "openai"),
])
def test_resolve_provider_from_url(url, expected):
    assert llmclient.resolve_provider(url) == expected


def test_explicit_provider_overrides_detection():
    """The one case a URL can't express: an Anthropic-format gateway on a
    neutral host, which would otherwise be assumed OpenAI-shaped."""
    assert llmclient.resolve_provider("https://llm.internal/v1", "anthropic") == "anthropic"
    assert llmclient.resolve_provider("https://api.anthropic.com", "openai") == "openai"
    assert llmclient.resolve_provider("https://llm.internal/v1", "auto") == "openai"


def test_unknown_provider_is_rejected_loudly():
    """A typo in CI_LLM_PROVIDER must not silently fall back to a provider
    the deployer didn't choose — that would send their prompts somewhere
    they didn't intend."""
    with pytest.raises(ValueError, match="unknown CI_LLM_PROVIDER"):
        llmclient.resolve_provider("https://x/v1", "bedrok")


def test_build_client_picks_the_wire_for_the_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    assert isinstance(llmclient.build_client(), llmclient.AnthropicClient)

    monkeypatch.setattr(config, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert isinstance(llmclient.build_client(), llmclient.OpenAIClient)

    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    client = llmclient.build_client()
    assert isinstance(client, llmclient.AnthropicClient) and client.provider == "bedrock"


# ── partial JSON (the OpenAI wire's streaming tool arguments) ─────────────

def test_partial_json_keeps_a_half_written_string_value():
    """The case the Composer's live preview is built on: `html` is written
    one fragment at a time and has to be showable before it is closed."""
    assert llmclient.parse_partial_json('{"name": "Q3", "html": "<div class=') == {
        "name": "Q3", "html": "<div class=",
    }


def test_partial_json_drops_a_half_written_key():
    assert llmclient.parse_partial_json('{"name": "Q3", "ht') == {"name": "Q3"}


@pytest.mark.parametrize("fragment,expected", [
    ("", {}),
    ("{", {}),
    ('{"a"', {}),
    ('{"a":', {}),
    ('{"a": ', {}),
    ('{"a": 1', {"a": 1}),          # a trailing number may still grow; shown anyway
    ('{"a": 1, ', {"a": 1}),
    ('{"a": tru', {}),              # an incomplete literal carries nothing
    ('{"a": true}', {"a": True}),
    ('{"a": [1, 2', {"a": [1, 2]}),
    ('{"a": [1, 2]', {"a": [1, 2]}),
    ('{"a": {"b": "c', {"a": {"b": "c"}}),
    # an array element that has only just opened shows up empty rather than
    # disappearing — a list growing by one item, which is what it is
    ('{"a": [{"b": 1}, {"b"', {"a": [{"b": 1}, {}]}),
    ('{"a": "x\\"y', {"a": 'x"y'}),
    ('{"a": "x\\', {"a": "x"}),     # a dangling escape must not eat the quote
    ("not json at all", {}),
    ("[1, 2]", {}),                 # tool arguments are always an object
])
def test_partial_json_edge_cases(fragment, expected):
    assert llmclient.parse_partial_json(fragment) == expected


def test_partial_json_matches_json_loads_at_every_prefix():
    """Every prefix of a real tool call must parse to *something* consistent:
    never an exception, never a value the finished document contradicts."""
    document = json.dumps({
        "cells": [{"target": "c1", "source": "lf = read('a.parquet')\nlf"}],
        "notes": "reads the file",
        "count": 12,
        "flag": True,
    })
    for i in range(len(document) + 1):
        partial = llmclient.parse_partial_json(document[:i])
        assert isinstance(partial, dict)
        for key, value in partial.items():
            if isinstance(value, str) and isinstance(json.loads(document)[key], str):
                # a partial string is always a prefix of the finished one
                assert json.loads(document)[key].startswith(value)
    assert llmclient.parse_partial_json(document) == json.loads(document)


def test_final_args_recovers_a_truncated_response():
    """A response cut short by max_tokens still yields what it managed to
    write — every field is re-validated upstream anyway, so a partial
    proposal beats an error page."""
    assert llmclient._final_args('{"reason_text": "not answerable') == {"reason_text": "not answerable"}
    assert llmclient._final_args("") == {}
    assert llmclient._final_args("garbage") == {}


# ── OpenAI wire: request translation ──────────────────────────────────────

_TOOL = {
    "name": "propose_query",
    "eager_input_streaming": True,           # Anthropic-only hint
    "description": "do the thing",
    "input_schema": {
        "type": "object",
        "properties": {"model": {"type": "string", "enum": ["sales"]},
                       "limit": {"type": ["integer", "null"]}},
        "required": ["model"],
    },
}


def _request(**overrides):
    return llmclient.ChatRequest(**{
        "model": "gpt-4o", "max_tokens": 512, "system": "SYS",
        "tools": [_TOOL], "prompt": "PROMPT", **overrides,
    })


def test_openai_tools_carry_the_schema_across_untouched():
    [tool] = llmclient._openai_tools([_TOOL])
    assert tool == {
        "type": "function",
        "function": {
            "name": "propose_query",
            "description": "do the thing",
            "parameters": _TOOL["input_schema"],
        },
    }


def test_openai_request_shape():
    kwargs = llmclient.OpenAIClient()._kwargs(_request())
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["max_tokens"] == 512
    assert kwargs["tool_choice"] == "required"
    assert kwargs["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "PROMPT"},
    ]


def test_openai_request_forces_a_named_tool_when_asked():
    kwargs = llmclient.OpenAIClient()._kwargs(_request(force_tool="describe_lineage"))
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "describe_lineage"}}


def test_cache_system_is_a_no_op_on_the_openai_wire():
    """Anthropic's explicit cache breakpoint has no OpenAI equivalent (its
    caching is automatic), so the flag must not leak into the request."""
    kwargs = llmclient.OpenAIClient()._kwargs(_request(cache_system=True))
    assert kwargs["messages"][0] == {"role": "system", "content": "SYS"}
    assert "cache_control" not in json.dumps(kwargs)


def test_reasoning_effort_only_for_declared_thinking_models(monkeypatch):
    monkeypatch.setattr(config, "LLM_THINKING_MODELS", {"gpt-5"})
    monkeypatch.setattr(config, "LLM_REASONING_EFFORT", "high")
    client = llmclient.OpenAIClient()
    assert client._kwargs(_request(model="gpt-5", thinking=True))["reasoning_effort"] == "high"
    assert "reasoning_effort" not in client._kwargs(_request(model="gpt-4o", thinking=True))
    assert "reasoning_effort" not in client._kwargs(_request(model="gpt-5", thinking=False))


# ── OpenAI wire: max_tokens vs max_completion_tokens ──────────────────────

@pytest.mark.parametrize("model,param", [
    ("gpt-4o", "max_tokens"),
    ("gpt-4.1", "max_tokens"),
    ("llama-3.3-70b", "max_tokens"),
    ("o3", "max_completion_tokens"),
    ("o4-mini", "max_completion_tokens"),
    ("gpt-5", "max_completion_tokens"),
])
def test_token_param_guessed_from_the_model_id(model, param, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_TOKENS_PARAM", "auto")
    assert llmclient.OpenAIClient()._max_tokens_param(model) == param


def test_token_param_can_be_pinned(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_TOKENS_PARAM", "max_completion_tokens")
    assert llmclient.OpenAIClient()._max_tokens_param("gpt-4o") == "max_completion_tokens"


def test_token_param_self_corrects_after_a_rejection(monkeypatch):
    """The safety net for a model id we can't recognize on a gateway we've
    never seen: the provider's own 400 names the parameter it wanted."""
    monkeypatch.setattr(config, "LLM_MAX_TOKENS_PARAM", "auto")
    client = llmclient.OpenAIClient()
    error = Exception("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead.")
    assert client._swap_token_param(error, "mystery-model") is True
    assert client._max_tokens_param("mystery-model") == "max_completion_tokens"
    # an unrelated 400 must not trigger a pointless retry
    assert client._swap_token_param(Exception("invalid api key"), "mystery-model") is False


# ── OpenAI wire: streaming ────────────────────────────────────────────────

class _Function:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, index=0, name=None, arguments=None):
        self.index = index
        self.function = _Function(name, arguments)


class _Chunk:
    def __init__(self, tool_calls=None, reasoning=None):
        delta = type("Delta", (), {"tool_calls": tool_calls, "reasoning_content": reasoning})()
        self.choices = [type("Choice", (), {"delta": delta})()]


def _stream_client(monkeypatch, chunks, captured=None):
    import openai

    class FakeCompletions:
        def create(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)
            return iter(chunks)

    class FakeClient:
        def __init__(self, **kwargs):
            if captured is not None:
                captured["_client_kwargs"] = kwargs
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    return llmclient.OpenAIClient(api_key="k", base_url="https://gateway/v1")


def test_openai_stream_reassembles_fragments_into_live_partial_dicts(monkeypatch):
    """The behaviour the Anthropic SDK gives us for free and this wire has to
    reconstruct: a caller watching `tool_input` sees the arguments grow."""
    chunks = [
        _Chunk([_ToolCallDelta(name="write_cells")]),
        _Chunk([_ToolCallDelta(arguments='{"notes": "reads')]),
        _Chunk([_ToolCallDelta(arguments=' the file", "cells": [')]),
        _Chunk([_ToolCallDelta(arguments='{"target": "c1", "source": "lf"}]}')]),
    ]
    client = _stream_client(monkeypatch, chunks)
    events = list(client.stream(_request()))

    assert events[0].kind == "tool_name" and events[0].tool_name == "write_cells"
    assert [e.tool_input for e in events if e.kind == "tool_input"] == [
        {"notes": "reads"},
        {"notes": "reads the file", "cells": []},
        {"notes": "reads the file", "cells": [{"target": "c1", "source": "lf"}]},
    ]
    assert events[-1].kind == "done"
    assert events[-1].final == llmclient.ToolCall(
        name="write_cells",
        args={"notes": "reads the file", "cells": [{"target": "c1", "source": "lf"}]},
    )


def test_openai_stream_surfaces_reasoning_when_a_gateway_sends_it(monkeypatch):
    chunks = [
        _Chunk(reasoning="weighing the options"),
        _Chunk([_ToolCallDelta(name="answer", arguments='{"text": "hi"}')]),
    ]
    events = list(_stream_client(monkeypatch, chunks).stream(_request()))
    assert events[0].kind == "thinking" and events[0].text == "weighing the options"


def test_openai_stream_ignores_a_parallel_second_tool_call(monkeypatch):
    """These prompts always want exactly one decision; a model that emits two
    anyway must not have its argument buffers interleaved into nonsense."""
    chunks = [
        _Chunk([_ToolCallDelta(index=0, name="answer", arguments='{"text": "a"}')]),
        _Chunk([_ToolCallDelta(index=1, name="write_cells", arguments='{"cells": []}')]),
    ]
    events = list(_stream_client(monkeypatch, chunks).stream(_request()))
    assert [e.kind for e in events if e.kind == "tool_name"] == ["tool_name"]
    assert events[-1].final == llmclient.ToolCall(name="answer", args={"text": "a"})


def test_openai_stream_without_a_tool_call_is_an_error(monkeypatch):
    """Forced tool use means prose is a failure, not a fallback — the caller
    has nothing to validate or execute."""
    with pytest.raises(llmclient.LLMError, match="did not call any tool"):
        list(_stream_client(monkeypatch, [_Chunk()]).stream(_request()))


def test_openai_stream_tolerates_a_usage_only_final_chunk(monkeypatch):
    """Many gateways end a stream with a chunk carrying no choices at all."""
    chunks = [
        _Chunk([_ToolCallDelta(name="answer", arguments='{"text": "hi"}')]),
        type("UsageChunk", (), {"choices": []})(),
    ]
    events = list(_stream_client(monkeypatch, chunks).stream(_request()))
    assert events[-1].final.args == {"text": "hi"}


def test_openai_errors_become_llm_errors(monkeypatch):
    """Nothing above this module should ever have to import a provider SDK to
    catch its failures."""
    import openai

    class FakeCompletions:
        def create(self, **kwargs):
            raise openai.APIConnectionError(request=None)

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    with pytest.raises(llmclient.LLMError):
        list(llmclient.OpenAIClient(api_key="k").stream(_request()))


# ── transport wiring ──────────────────────────────────────────────────────

def test_openai_client_passes_base_url_and_key(monkeypatch):
    captured = {}
    client = _stream_client(monkeypatch, [_Chunk([_ToolCallDelta(name="answer", arguments="{}")])], captured)
    list(client.stream(_request()))
    assert captured["_client_kwargs"] == {"api_key": "k", "base_url": "https://gateway/v1"}


def test_local_endpoints_need_no_key(monkeypatch):
    """Ollama/vLLM want no key at all, but the SDK refuses to construct
    without one."""
    captured = {}
    import openai

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = type("Chat", (), {"completions": type("C", (), {"create": lambda s, **k: iter(())})()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    llmclient.OpenAIClient(api_key="", base_url="http://localhost:11434/v1")._client()
    assert captured["api_key"] == "not-required"


def test_azure_uses_the_dated_surface_only_when_api_version_is_set(monkeypatch):
    """Azure's newer /openai/v1/ endpoint is plain URL+key; the dated
    deployment surface needs the SDK to build its paths."""
    import openai

    seen = {}

    def fake_azure(**kwargs):
        seen.update(kwargs)
        return "azure-client"

    def fake_openai(**kwargs):
        seen.update(kwargs)
        return "plain-client"

    monkeypatch.setattr(openai, "AzureOpenAI", fake_azure)
    monkeypatch.setattr(openai, "OpenAI", fake_openai)

    v1_url = "https://acme.openai.azure.com/openai/v1/"
    monkeypatch.setattr(config, "LLM_API_VERSION", "")
    assert llmclient.OpenAIClient(api_key="k", base_url=v1_url, provider="azure")._client() == "plain-client"
    assert seen == {"api_key": "k", "base_url": v1_url}

    # the dated surface takes the bare resource root and builds its own paths
    seen.clear()
    root = "https://acme.openai.azure.com"
    monkeypatch.setattr(config, "LLM_API_VERSION", "2024-10-21")
    assert llmclient.OpenAIClient(api_key="k", base_url=root, provider="azure")._client() == "azure-client"
    assert seen == {"azure_endpoint": root, "api_key": "k", "api_version": "2024-10-21"}


def test_bedrock_uses_the_sigv4_client_and_no_api_key(monkeypatch):
    """Native Bedrock authenticates from the AWS credential chain — an IAM
    role, typically — so there is no key to pass and none must be invented."""
    import anthropic

    seen = {}

    def fake_bedrock(**kwargs):
        seen.update(kwargs)
        return "bedrock-client"

    monkeypatch.setattr(anthropic, "AnthropicBedrock", fake_bedrock)
    monkeypatch.setattr(config, "LLM_AWS_REGION", "eu-west-1")
    assert llmclient.AnthropicClient(provider="bedrock")._client() == "bedrock-client"
    assert seen == {"aws_region": "eu-west-1"}


def test_thinking_means_the_same_thing_on_both_wires_and_both_methods(monkeypatch):
    """`thinking` is one flag with one meaning: honoring it on one wire's
    streamed path but dropping it on the other's non-streamed path is the
    kind of asymmetry that only shows up as a mystery quality difference."""
    import anthropic

    monkeypatch.setattr(config, "LLM_THINKING_MODELS", {"claude-sonnet-5"})
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return type("Message", (), {"content": []})()

    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda **kwargs: type("Client", (), {"messages": FakeMessages()})())

    request = _request(model="claude-sonnet-5", thinking=True)
    with pytest.raises(llmclient.LLMError):        # no tool_use block in the reply
        llmclient.AnthropicClient(api_key="k").call(request)
    assert seen["thinking"] == {"type": "adaptive", "display": "summarized"}

    seen.clear()
    with pytest.raises(llmclient.LLMError):
        llmclient.AnthropicClient(api_key="k").call(_request(model="claude-sonnet-5"))
    assert "thinking" not in seen


def test_anthropic_client_passes_base_url_when_set(monkeypatch):
    """An Anthropic-format gateway is reachable with nothing but a URL."""
    import anthropic

    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: seen.update(kwargs) or "client")
    llmclient.AnthropicClient(api_key="k", base_url="https://llm.internal/v1")._client()
    assert seen == {"api_key": "k", "base_url": "https://llm.internal/v1"}

    seen.clear()
    llmclient.AnthropicClient(api_key="k")._client()
    assert seen == {"api_key": "k"}


# ── config derivation (what a deployer's env actually turns into) ─────────
#
# config.py reads the environment once at import, so these reload it. The
# fixture puts it back afterwards — every other module holds a reference to
# this same module object.

_LLM_ENV = (
    "CI_LLM_API_KEY", "CI_LLM_BASE_URL", "CI_LLM_PROVIDER", "CI_LLM_MODEL",
    "CI_LLM_MODEL_CHOICES", "CI_LLM_THINKING_MODELS", "CI_SANDBOX_LINEAGE_MODEL",
)


@pytest.fixture
def reloaded_config(monkeypatch):
    import importlib

    def reload(**env):
        for key in _LLM_ENV:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield reload
    monkeypatch.undo()
    importlib.reload(config)


def test_claude_defaults_survive_when_nothing_is_configured(reloaded_config):
    cfg = reloaded_config()
    assert cfg.LLM_MODEL == "claude-sonnet-5"
    assert cfg.LLM_MODEL_CHOICES == ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    assert cfg.LLM_THINKING_MODELS == {"claude-opus-4-8", "claude-sonnet-5"}
    assert cfg.LLM_ENABLED is False


def test_pointing_at_another_provider_drops_the_claude_model_ids(reloaded_config):
    """The bug this prevents: CI_LLM_MODEL=gpt-4o with the Claude list still
    in place offers a model picker whose other two entries 400 on every
    request, and hands the lineage helper a Claude id no OpenAI endpoint
    serves."""
    cfg = reloaded_config(CI_LLM_API_KEY="k", CI_LLM_BASE_URL="https://api.openai.com/v1",
                          CI_LLM_MODEL="gpt-4o")
    assert cfg.LLM_MODEL_CHOICES == ["gpt-4o"]
    assert cfg.LLM_THINKING_MODELS == set()
    assert cfg.SANDBOX_LINEAGE_MODEL == "gpt-4o"
    assert cfg.LLM_ENABLED is True


def test_model_choices_and_thinking_models_are_configurable(reloaded_config):
    cfg = reloaded_config(CI_LLM_API_KEY="k", CI_LLM_MODEL="gpt-4o",
                          CI_LLM_MODEL_CHOICES="gpt-4o, gpt-5, o3",
                          CI_LLM_THINKING_MODELS="gpt-5,o3")
    assert cfg.LLM_MODEL_CHOICES == ["gpt-4o", "gpt-5", "o3"]
    assert cfg.LLM_THINKING_MODELS == {"gpt-5", "o3"}


def test_the_default_model_is_always_selectable(reloaded_config):
    """Every request echoes the conversation's model back for validation, so
    a default missing from the choices would 400 on its own default."""
    cfg = reloaded_config(CI_LLM_API_KEY="k", CI_LLM_MODEL="gpt-5", CI_LLM_MODEL_CHOICES="gpt-4o")
    assert cfg.LLM_MODEL_CHOICES == ["gpt-5", "gpt-4o"]


def test_thinking_can_be_turned_off_entirely(reloaded_config):
    """An explicitly empty list means none, not "fall back to the default" —
    the escape hatch for a gateway that rejects the parameter."""
    cfg = reloaded_config(CI_LLM_API_KEY="k", CI_LLM_THINKING_MODELS="")
    assert cfg.LLM_THINKING_MODELS == set()


def test_bedrock_is_enabled_without_an_api_key(reloaded_config):
    """Native Bedrock authenticates by IAM role, so "no key" is a valid,
    fully-configured deployment there — unlike everywhere else, where it
    means the LLM features stay off."""
    assert reloaded_config(CI_LLM_PROVIDER="bedrock").LLM_ENABLED is True
    assert reloaded_config(CI_LLM_BASE_URL="https://api.openai.com/v1").LLM_ENABLED is False
