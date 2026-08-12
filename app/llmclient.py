"""The one module that knows which *wire format* the configured LLM endpoint
speaks. Everything above it (app/llm.py, app/sandbox_agent.py, app/composer.py)
builds the same provider-neutral `ChatRequest` and consumes the same
`ClientEvent` stream, so adding a provider never touches a prompt, a tool
schema, or a validation path.

All three seams above ask the model for exactly one thing: *call one of these
tools, with these arguments*. That is the entire contract this module has to
port across providers — no multi-turn tool loops, no assistant message
history, no server-side state. Which is why "point it at a URL and a key"
actually works here.

Two wire formats cover every target we care about:

  provider    | wire      | auth                    | typical base_url
  ------------|-----------|-------------------------|---------------------------
  anthropic   | Anthropic | x-api-key               | (default) api.anthropic.com
  bedrock     | Anthropic | AWS SigV4 (boto3 chain) | bedrock-runtime.<region>...
  openai      | OpenAI    | Authorization: Bearer   | anything else
  azure       | OpenAI    | api-key / Bearer        | <res>.openai.azure.com/openai/v1/

`bedrock` is the one target that genuinely cannot be reduced to URL+key: it
signs every request with SigV4 from the standard AWS credential chain (env
vars, instance/task role, SSO profile), which is also the reason most AWS
deployments *want* it — no static key to distribute. The URL+key path into
Bedrock is its OpenAI-compatible surface
(https://bedrock-runtime.<region>.amazonaws.com/openai/v1) with a Bedrock API
key, which auto-detects to the `openai` wire below and needs nothing special.

Provider detection is by URL (resolve_provider) with CI_LLM_PROVIDER as the
override, because a URL is the only thing a deployer reliably has. An
unrecognized host is assumed to be OpenAI-shaped, by far the more common
gateway dialect (vLLM, Ollama, LiteLLM, OpenRouter, Together, Groq) — which
leaves two cases a hostname alone cannot express:

  * a gateway speaking the *Anthropic* format on a neutral host, which needs
    CI_LLM_PROVIDER=anthropic;
  * a corporate gateway fronting *Azure* (gateway.example.net/azure-openai),
    where the deployment name belongs in the path. Setting CI_LLM_API_VERSION
    is enough to detect that one, since an api-version means nothing to any
    other provider.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol
from urllib.parse import urlparse

from . import config

logger = logging.getLogger(__name__)

Provider = Literal["anthropic", "openai", "azure", "bedrock"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai", "azure", "bedrock")

# Which wire format each provider speaks — the only distinction that matters
# below the transport details (auth, base_url, extra params).
_ANTHROPIC_WIRE = {"anthropic", "bedrock"}


class LLMError(Exception):
    """The LLM call itself failed (network/timeout/API error, or the model
    returned no tool call at all). Each seam above catches this and re-raises
    its own feature-specific error (TranslatorError / AgentError /
    ComposerError), so nothing above this module ever sees a provider SDK's
    exception type."""


@dataclass(frozen=True)
class ToolCall:
    """The model's unvalidated decision: which tool, and that tool's raw
    arguments. Unvalidated is the whole point — every caller re-checks these
    args against live server-side state before they can do anything."""
    name: str
    args: dict


@dataclass(frozen=True)
class ClientEvent:
    """One incremental update from LLMClient.stream(). Everything but "done"
    is display-only; only `final` is acted on (and even that is re-validated
    upstream), so the two wire formats are free to differ in how much detail
    they can actually stream."""
    kind: Literal["thinking", "tool_name", "tool_input", "done"]
    text: str = ""                      # kind="thinking"
    tool_name: str | None = None        # kind="tool_name"
    tool_input: dict | None = None      # kind="tool_input": args parsed so far
    final: ToolCall | None = None       # kind="done"


@dataclass(frozen=True)
class ChatRequest:
    """One forced-tool-use call, in provider-neutral terms.

    `tools` stay in the Anthropic shape ({name, description, input_schema})
    because that is what all three seams already declare and what their tests
    assert on; the OpenAI adapter translates them (_openai_tools). Extra keys
    like `eager_input_streaming` are Anthropic-only hints and are dropped for
    other providers rather than leaking through as unknown parameters.
    """
    model: str
    max_tokens: int
    system: str
    tools: list[dict]
    prompt: str
    # None -> "call *some* tool" (Anthropic tool_choice "any" / OpenAI
    # "required"); a name -> that specific tool.
    force_tool: str | None = None
    # Ask for a cache breakpoint on the (long, static, resent-every-turn)
    # system prompt. Honored on the Anthropic wire; a no-op elsewhere, where
    # prompt caching is automatic and prefix-based rather than declared.
    cache_system: bool = False
    # Ask for extended thinking/reasoning. Only sent to models declared to
    # support it (config.LLM_THINKING_MODELS) — a model that doesn't support
    # it rejects the whole request rather than ignoring the parameter.
    thinking: bool = False


class LLMClient(Protocol):
    provider: str

    def call(self, req: ChatRequest) -> ToolCall: ...

    def stream(self, req: ChatRequest) -> Iterator[ClientEvent]: ...


# ── partial JSON (streaming tool arguments) ───────────────────────────────
#
# The Anthropic SDK accumulates and parses partial tool input for us
# (`event.snapshot` is already a dict). The OpenAI wire streams raw JSON
# *string* fragments instead, so to keep the same live-progress UX — the
# Composer showing HTML as it is written, the sandbox agent showing code
# appearing in a cell — we have to parse the incomplete buffer ourselves.

def parse_partial_json(text: str) -> dict:
    """Best-effort dict from an incomplete JSON object, for display only.

    Returns {} rather than raising for anything unsalvageable: a partial
    parse feeds a progress view, never a decision (the "done" event's fully
    parsed args are what callers act on).
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    else:
        return parsed if isinstance(parsed, dict) else {}
    try:
        parsed = json.loads(_repair_json(text))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _repair_json(text: str) -> str:
    """Close an incomplete JSON document at the last point it can be closed
    cleanly, keeping a partially-written *string value* (the interesting
    case: a half-written `html` or `source` field) but discarding a
    half-written key, number or literal, which carry nothing to show.

    Single pass: track the container stack and where the last complete value
    ended, then append the closers. No backtracking — this runs on every
    streamed fragment, so it has to stay linear in the buffer length.
    """
    stack: list[str] = []       # '{' / '[' still open
    safe = 0                    # length of the prefix that can be closed cleanly
    safe_stack: list[str] = []  # the container stack as of `safe`
    expect = "value"            # value | key | colon | comma
    in_string = False
    is_key = False
    escaped = False
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if is_key:
                    expect = "colon"
                else:
                    expect, safe, safe_stack = "comma", i + 1, list(stack)
            i += 1
            continue
        if ch in " \t\r\n":
            i += 1
        elif ch == '"':
            in_string, is_key = True, expect == "key"
            i += 1
        elif ch in "{[":
            stack.append(ch)
            expect = "key" if ch == "{" else "value"
            i += 1
            safe, safe_stack = i, list(stack)
        elif ch in "}]":
            if stack:
                stack.pop()
            expect = "comma"
            i += 1
            safe, safe_stack = i, list(stack)
        elif ch == ":":
            expect = "value"
            i += 1
        elif ch == ",":
            expect = "key" if stack and stack[-1] == "{" else "value"
            i += 1
        else:
            # a bare literal: number, true, false or null
            j = i
            while j < n and text[j] not in ' \t\r\n,}]':
                j += 1
            complete = j < n
            if not complete:
                # runs to the end of the buffer, so it may still grow ("12"
                # becoming "125"). Included anyway if it parses on its own —
                # this is a display-only view that the next fragment
                # corrects, and dropping it would flicker the field in and
                # out instead. An unparseable stub ("tru") carries nothing.
                try:
                    json.loads(text[i:j])
                except ValueError:
                    complete = False
                else:
                    complete = True
            if complete:
                expect, safe, safe_stack = "comma", j, list(stack)
            i = j

    if in_string and not is_key:
        # keep the partial string value, closing the quote (and dropping a
        # dangling backslash, which would escape the quote we just added)
        head = text[:-1] if escaped else text
        return head + '"' + _closers(stack)
    return text[:safe] + _closers(safe_stack)


def _closers(stack: list[str]) -> str:
    return "".join("}" if c == "{" else "]" for c in reversed(stack))


def _final_args(raw: str) -> dict:
    """The finished tool arguments. Falls back to a partial parse when the
    response was cut short (max_tokens, a dropped connection) rather than
    failing outright: a truncated proposal is still re-validated upstream
    like any other, and a partial page/query beats an error page."""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        parsed = parse_partial_json(raw)
        if parsed:
            logger.warning("tool arguments were truncated/invalid JSON; recovered %d field(s)", len(parsed))
        else:
            logger.warning("tool arguments could not be parsed at all (%d chars)", len(raw or ""))
        return parsed
    return parsed if isinstance(parsed, dict) else {}


# ── provider detection ────────────────────────────────────────────────────

def resolve_provider(base_url: str, override: str = "", api_version: str = "") -> Provider:
    """Which provider a base URL points at, unless CI_LLM_PROVIDER says
    otherwise. No URL at all means the historical default (Anthropic's own
    API); an unrecognized host means the OpenAI wire, which is what
    essentially every third-party gateway and self-hosted server speaks."""
    override = (override or "").strip().lower()
    if override and override != "auto":
        if override not in PROVIDERS:
            raise ValueError(f"unknown CI_LLM_PROVIDER {override!r} (choose one of {', '.join(PROVIDERS)}, or 'auto')")
        return override  # type: ignore[return-value]
    if not (base_url or "").strip():
        return "anthropic"
    parsed = urlparse(base_url if "//" in base_url else f"https://{base_url}")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host.endswith("anthropic.com"):
        return "anthropic"
    if (api_version or "").strip():
        # an api-version is an Azure-only concept, so setting one is a
        # deliberate enough signal to detect on. It has to be, because the
        # hostname often can't be: a corporate API gateway fronting Azure
        # (gateway.example.net/azure-openai) looks exactly like any other
        # OpenAI-shaped gateway, and guessing OpenAI there sends requests to
        # /chat/completions instead of /openai/deployments/<model>/chat/
        # completions — a 404 from a perfectly good URL and key.
        return "azure"
    if "azure.com" in host or "azure-api.net" in host:
        return "azure"
    if "bedrock" in host:
        # Bedrock's own OpenAI-compatible surface (.../openai/v1) is plain
        # bearer-token OpenAI; the native surface needs SigV4 signing.
        return "openai" if "/openai" in path else "bedrock"
    return "openai"


def configured_provider() -> str:
    """The resolved provider name, for diagnostics (GET /api/health). Never
    raises: a typo in CI_LLM_PROVIDER surfaces here as "invalid" rather than
    breaking the health check that would tell a deployer about it — the first
    real request still fails loudly."""
    try:
        return resolve_provider(config.LLM_BASE_URL, config.LLM_PROVIDER, config.LLM_API_VERSION)
    except ValueError:
        return "invalid"


def build_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
) -> LLMClient:
    """The configured client. Constructing one is free — the provider SDK is
    imported lazily, on the first actual request — so seams can build theirs
    at import time exactly as they always have."""
    key = config.LLM_API_KEY if api_key is None else api_key
    url = (config.LLM_BASE_URL if base_url is None else base_url or "").strip()
    resolved = resolve_provider(
        url, config.LLM_PROVIDER if provider is None else provider, config.LLM_API_VERSION
    )
    if resolved in _ANTHROPIC_WIRE:
        return AnthropicClient(api_key=key, base_url=url, provider=resolved)
    return OpenAIClient(api_key=key, base_url=url, provider=resolved)


def key_fingerprint(api_key: str) -> str:
    """A stable, non-reversible label for a key, so "but that key works
    elsewhere" becomes a comparison instead of an argument — without a secret
    ever reaching a log file.

    The failure this exists for is a key that is *correct at the source* and
    mangled in transit: truncated by shell expansion, quoted into the value,
    line-wrapped, or carrying a stray CR. All of those produce an ordinary
    401, and none of them are visible from one. Reproduce it next to a
    working client to compare:

        python -c "import hashlib,sys; print(len(sys.argv[1]), hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])" "$KEY"
    """
    if not api_key:
        return "(none)"
    return f"len={len(api_key)} sha256={hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:8]}"


def _no_tool_call(reason: str | None, model: str, provider: str = "openai") -> LLMError:
    """Forced tool use produced no tool call, and *why* matters enormously.

    "Ran out of budget mid-thought" is a settings problem with a one-line
    fix, but it reaches the user as the same "model did not call any tool" as
    a model that genuinely declined — which sends whoever is debugging it to
    the prompts instead of to the one number that fixes it. `reason` is the
    normalized stop reason ("length" on either wire).
    """
    if reason == "length":
        # by wire, not by provider name: "azure" and "openai" share the
        # pooled-budget parameter, and only one of them is spelled "openai"
        if provider not in _ANTHROPIC_WIRE:
            logger.warning(
                "%s stopped at its token budget before calling a tool. max_completion_tokens "
                "covers reasoning tokens as well as the answer, and a reasoning model can "
                "spend the whole allowance thinking: raise CI_LLM_REASONING_TOKENS "
                "(currently %d) or ask for less reasoning with CI_LLM_REASONING_EFFORT=low.",
                model, config.LLM_REASONING_TOKENS,
            )
        else:
            logger.warning(
                "%s hit max_tokens before calling a tool — extended thinking draws on the "
                "same budget as the answer, so the seam's limit has to cover both.", model,
            )
        return LLMError("the model used its whole token budget before calling a tool")
    if reason == "content_filter":
        return LLMError("the request was stopped by a content filter")
    logger.warning("model %s returned no tool call (stop reason=%s)", model, reason)
    return LLMError("model did not call any tool")


def _wrap(exc: Exception, provider: str = "", key_fp: str = "") -> LLMError:
    """Shared failure path for every provider: the user only ever sees a
    generic "temporarily unavailable" message, so log the real cause
    server-side — a deployer debugging a bad key, a wrong base URL or a proxy
    should not have to stare at "Connection error." with nothing else.

    The URL comes off the exception's own httpx request rather than from
    self.base_url, so it is the path actually sent (which for Azure the SDK
    builds, and which is exactly what a 404 is complaining about)."""
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    logger.warning(
        "LLM API call failed: %r (provider=%s, url=%s, cause: %r)",
        exc, provider or "?", url or "(request not sent)", exc.__cause__,
    )
    if getattr(exc, "status_code", None) in (401, 403):
        # The key is very often right and merely arrived damaged, so lead with
        # what the key we actually sent looked like — that is the one fact a
        # 401 withholds and the deployer can check against a working client.
        logger.warning(
            "authentication failed with key %s. A 401 cannot distinguish a wrong key from "
            "a correct one damaged in transit, so compare that fingerprint against the key "
            "at its source; if they differ, suspect a $ in the value (both the shell and "
            "docker compose interpolate it), wrapping quotes kept as part of the value, or "
            "a line break. If they match, the key really is being rejected — check that it "
            "is entitled to this deployment (%s) on this endpoint.",
            key_fp or "(unknown)", config.LLM_MODEL,
        )
    if getattr(exc, "status_code", None) == 404 and provider == "openai":
        # By far the most likely misconfiguration behind a 404 on a URL the
        # deployer copied from somewhere that worked: an Azure OpenAI
        # endpoint, or a gateway fronting one, detected as a plain OpenAI
        # gateway because its hostname says nothing about Azure.
        logger.warning(
            "a 404 on the OpenAI wire usually means the endpoint is an Azure "
            "OpenAI deployment (or a gateway fronting one): its chat path is "
            "/openai/deployments/<model>/chat/completions?api-version=..., not "
            "/chat/completions. Set CI_LLM_API_VERSION (and CI_LLM_PROVIDER=azure) "
            "to switch to it."
        )
    return LLMError(str(exc))


# ── Anthropic wire (api.anthropic.com, Bedrock, Anthropic-format gateways) ─

def _anthropic_reason(message) -> str | None:
    """Anthropic's stop_reason in the OpenAI wire's vocabulary, so one
    exhausted budget reads the same whichever endpoint hit it."""
    stop = getattr(message, "stop_reason", None)
    return "length" if stop == "max_tokens" else stop


class AnthropicClient:
    """Anthropic Messages API with forced tool use. `base_url` covers any
    Anthropic-compatible endpoint; provider="bedrock" swaps in the SigV4
    client, which reads AWS credentials from the standard boto3 chain (env
    vars, instance/task role, SSO profile) rather than from an API key."""

    def __init__(self, api_key: str = "", base_url: str = "", provider: str = "anthropic"):
        self.api_key = api_key
        self.base_url = base_url
        self.provider = provider

    def _client(self):
        import anthropic

        if self.provider == "bedrock":
            kwargs = {"aws_region": config.LLM_AWS_REGION}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return anthropic.AnthropicBedrock(**kwargs)
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return anthropic.Anthropic(**kwargs)

    def _system(self, req: ChatRequest):
        if not req.cache_system:
            return req.system
        # one explicit cache breakpoint on a long, static, every-turn prompt
        return [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]

    def _kwargs(self, req: ChatRequest) -> dict:
        tool_choice = (
            {"type": "tool", "name": req.force_tool} if req.force_tool else {"type": "any"}
        )
        return dict(
            model=req.model,
            max_tokens=req.max_tokens,
            system=self._system(req),
            tools=req.tools,
            tool_choice=tool_choice,
            messages=[{"role": "user", "content": req.prompt}],
        )

    def _thinking_kwargs(self, req: ChatRequest) -> dict:
        """The `thinking` kwarg, omitted entirely for a model that doesn't
        support adaptive thinking rather than sent unconditionally and left
        to 400 with "adaptive thinking is not supported on this model"."""
        if req.thinking and req.model in config.LLM_THINKING_MODELS:
            return {"thinking": {"type": "adaptive", "display": "summarized"}}
        return {}

    def call(self, req: ChatRequest) -> ToolCall:
        import anthropic

        try:
            response = self._client().messages.create(
                **self._kwargs(req), **self._thinking_kwargs(req)
            )
        except anthropic.AnthropicError as exc:
            raise _wrap(exc, self.provider, key_fingerprint(self.api_key)) from exc
        for block in response.content:
            if block.type == "tool_use":
                return ToolCall(name=block.name, args=block.input or {})
        raise _no_tool_call(_anthropic_reason(response), req.model, self.provider)

    def stream(self, req: ChatRequest) -> Iterator[ClientEvent]:
        import anthropic

        try:
            with self._client().messages.stream(
                **self._kwargs(req), **self._thinking_kwargs(req)
            ) as stream:
                for event in stream:
                    if event.type == "thinking":
                        yield ClientEvent(kind="thinking", text=event.thinking)
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield ClientEvent(kind="tool_name", tool_name=event.content_block.name)
                    elif event.type == "input_json":
                        # eager_input_streaming on every tool means the SDK
                        # hands us an already-parsed partial dict here
                        snapshot = event.snapshot if isinstance(event.snapshot, dict) else {}
                        yield ClientEvent(kind="tool_input", tool_input=snapshot)
                message = stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise _wrap(exc, self.provider, key_fingerprint(self.api_key)) from exc

        for block in message.content:
            if block.type == "tool_use":
                yield ClientEvent(kind="done", final=ToolCall(name=block.name, args=block.input or {}))
                return
        raise _no_tool_call(_anthropic_reason(message), req.model, self.provider)


# ── OpenAI wire (OpenAI, Azure, Bedrock's /openai surface, any gateway) ────

# Reasoning models renamed max_tokens -> max_completion_tokens and reject the
# old spelling; everything else still wants max_tokens. The prefix match is a
# guess for the models we can recognize, and _swap_token_param below is the
# safety net for the ones we can't (any gateway, any model name).
_REASONING_MODEL_RE = re.compile(r"^(o[1-9]|gpt-[5-9])", re.IGNORECASE)


def _openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic-shaped tool declarations -> OpenAI function tools. The JSON
    Schema itself carries over untouched (strict mode is deliberately not
    requested: these schemas use `oneOf` and nullable unions, which strict
    mode forbids, and every argument is re-validated server-side anyway).
    Anthropic-only hints like `eager_input_streaming` are dropped rather than
    forwarded as unknown parameters."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


class OpenAIClient:
    """OpenAI chat-completions with forced tool use — the format spoken by
    OpenAI, Azure OpenAI, Bedrock's /openai/v1 surface, and effectively every
    gateway and self-hosted server (vLLM, Ollama, LiteLLM, OpenRouter, …).

    Deliberately chat-completions rather than the Responses API: it is the
    format every third-party implementation actually supports, and this seam
    needs nothing Responses adds.
    """

    def __init__(self, api_key: str = "", base_url: str = "", provider: str = "openai"):
        self.api_key = api_key
        self.base_url = base_url
        self.provider = provider
        self._token_param = ""   # learned lazily; see _swap_token_param

    def _client(self):
        import openai

        if self.provider == "azure" and config.LLM_API_VERSION:
            # The dated api-version surface, where the deployment name lives
            # in the path — the SDK builds that URL for us, so the base URL
            # here must be the bare resource root
            # (https://<resource>.openai.azure.com). Azure's newer
            # /openai/v1/ surface needs none of this and works as a plain
            # base_url below, which is why api-version is opt-in.
            return openai.AzureOpenAI(
                azure_endpoint=self.base_url,
                api_key=self.api_key,
                api_version=config.LLM_API_VERSION,
            )
        return openai.OpenAI(
            # a local server (Ollama, vLLM) usually wants no key at all, but
            # the SDK refuses to construct without one
            api_key=self.api_key or "not-required",
            base_url=self.base_url or None,
        )

    def _max_tokens_param(self, model: str) -> str:
        if self._token_param:
            return self._token_param
        if config.LLM_MAX_TOKENS_PARAM not in ("", "auto"):
            return config.LLM_MAX_TOKENS_PARAM
        return "max_completion_tokens" if _REASONING_MODEL_RE.match(model or "") else "max_tokens"

    def _swap_token_param(self, exc: Exception, model: str) -> bool:
        """A 400 that names one of the two token parameters means we guessed
        the wrong spelling for this model. Remember the other one on this
        client and let the caller retry once — the alternative is asking
        every deployer to know which of their gateway's models are reasoning
        models."""
        message = str(exc)
        if "max_tokens" not in message and "max_completion_tokens" not in message:
            return False
        current = self._max_tokens_param(model)
        self._token_param = "max_tokens" if current == "max_completion_tokens" else "max_completion_tokens"
        logger.info("retrying with %s instead of %s (provider rejected it)", self._token_param, current)
        return True

    def _kwargs(self, req: ChatRequest) -> dict:
        kwargs = dict(
            model=req.model,
            messages=[
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
            tools=_openai_tools(req.tools),
            tool_choice=(
                {"type": "function", "function": {"name": req.force_tool}}
                if req.force_tool else "required"
            ),
        )
        param = self._max_tokens_param(req.model)
        # The budget each seam asks for is how much *answer* it needs (1024
        # for a query proposal, 8192 for a composed page) — which is what
        # Anthropic's max_tokens means. max_completion_tokens does not: it
        # covers the model's reasoning tokens as well, and a reasoning model
        # will happily spend the entire allowance thinking and stop before it
        # ever emits the tool call. So add headroom on exactly the wire where
        # the two are pooled, keeping the callers' number meaning one thing.
        kwargs[param] = req.max_tokens + (
            config.LLM_REASONING_TOKENS if param == "max_completion_tokens" else 0
        )
        if req.thinking and req.model in config.LLM_THINKING_MODELS and config.LLM_REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.LLM_REASONING_EFFORT
        return kwargs

    def _create(self, req: ChatRequest, *, stream: bool):
        import openai

        client = self._client()
        for attempt in (0, 1):
            try:
                return client.chat.completions.create(stream=stream, **self._kwargs(req))
            except openai.BadRequestError as exc:
                if attempt == 0 and self._swap_token_param(exc, req.model):
                    continue
                raise _wrap(exc, self.provider, key_fingerprint(self.api_key)) from exc
            except openai.OpenAIError as exc:
                raise _wrap(exc, self.provider, key_fingerprint(self.api_key)) from exc
        raise LLMError("unreachable")   # pragma: no cover

    def call(self, req: ChatRequest) -> ToolCall:
        response = self._create(req, stream=False)
        finish_reason = None
        for choice in getattr(response, "choices", None) or []:
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            for tool_call in getattr(choice.message, "tool_calls", None) or []:
                function = getattr(tool_call, "function", None)
                if function is not None and function.name:
                    return ToolCall(name=function.name, args=_final_args(function.arguments or "{}"))
        raise _no_tool_call(finish_reason, req.model, self.provider)

    def stream(self, req: ChatRequest) -> Iterator[ClientEvent]:
        """Same call as call(), yielding events as the arguments arrive.

        Unlike the Anthropic wire, arguments come through as raw JSON string
        fragments, so the partial dicts the UI shows are reassembled here
        (parse_partial_json). Only the first tool call is followed: these
        prompts always want exactly one decision, and a model that emits
        parallel calls anyway would otherwise interleave two argument
        buffers into nonsense.
        """
        import openai

        stream = self._create(req, stream=True)
        name: str | None = None
        index: int | None = None
        buffer = ""
        finish_reason: str | None = None
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue                       # e.g. a usage-only final chunk
                finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                # Not in the OpenAI spec, but what reasoning models served
                # through vLLM/OpenRouter/DeepSeek-compatible gateways emit;
                # display-only, so surfacing it when present costs nothing.
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if isinstance(reasoning, str) and reasoning:
                    yield ClientEvent(kind="thinking", text=reasoning)
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    position = getattr(tool_call, "index", 0) or 0
                    if index is None:
                        index = position
                    elif position != index:
                        continue                   # a parallel call — ignored
                    function = getattr(tool_call, "function", None)
                    if function is None:
                        continue
                    if name is None and getattr(function, "name", None):
                        name = function.name
                        yield ClientEvent(kind="tool_name", tool_name=name)
                    fragment = getattr(function, "arguments", None)
                    if fragment:
                        buffer += fragment
                        yield ClientEvent(kind="tool_input", tool_input=parse_partial_json(buffer))
        except openai.OpenAIError as exc:
            raise _wrap(exc, self.provider, key_fingerprint(self.api_key)) from exc

        if not name:
            raise _no_tool_call(finish_reason, req.model, self.provider)
        yield ClientEvent(kind="done", final=ToolCall(name=name, args=_final_args(buffer)))
