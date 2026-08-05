# Quickstart: Agent & Skills Framework with MCP Server

Manual validation of this feature (Principle IV, adapted per plan.md — the
golden path here is a real MCP client, not a browser). Assumes the demo app
is already runnable per the root README.

## Prerequisites

1. `CI_LLM_API_KEY` set (conversational analytics, and therefore
   `ask_question`, is off/`outcome: "error"` without it — same as today).
2. App running with demo data seeded (`docker compose up`, or `./run.sh`
   locally).
3. A viewer (or higher) account and either its session cookie or a
   per-user API token (**Account → tokens**, same mechanism scripts already
   use — see README's "Authoring model measures" section).
4. An MCP client that can connect over Streamable HTTP with a bearer token
   — either `fastmcp`'s own `Client` (`pip install fastmcp`) for a scripted
   check, or a real external client (Claude Desktop/Code) configured to
   reach `http://<host>/mcp` with an `Authorization: Bearer <token>` header.

```bash
export CI_LLM_API_KEY=sk-ant-...
docker compose up
```

## Golden path (User Story 1 — ask a question from outside the browser)

1. Connect an MCP client to `/mcp` with a valid viewer token.
2. Call tool `ask_question` with `{"question": "what were total sales by
   region last quarter?"}`.
3. **Expect**: a result with `outcome: "answered"`, a `result` table broken
   down by region, and a `conversation_id` in the response.
4. Call `ask_question` again with that `conversation_id` and a follow-up
   question ("now break that down by month instead"). **Expect**: it
   reuses the `sales` model from the prior turn, same as the browser chat's
   follow-up behavior (spec 012).
5. Sign into the browser as the same user, open **Chat**. **Expect**: the
   conversation from steps 2-4 is visible there too — same store, same
   data, proving `ask_question` didn't create a parallel, invisible
   conversation concept.

## Ambiguous / out-of-scope questions

6. Ask something ambiguous between two models. **Expect**:
   `outcome: "clarification"` naming real candidates.
7. Ask something the semantic layer cannot answer. **Expect**:
   `outcome: "declined"` with a plain-language reason — never raw data,
   never a fabricated answer.

## LLM backend unavailable (covered by tests, not manual — see T018)

7a. Restart the app with `CI_LLM_API_KEY` unset and call `ask_question`.
    **Expect**: `outcome: "error"` immediately (same message class as the
    existing chat 503), not a hang or an unhandled exception. Covered by
    an automated test rather than this manual walkthrough since it
    requires a second app instance/config — see tasks.md T018.

## Discovery (User Story 2)

8. Call `tools/list` (or whatever your MCP client calls its "list tools"
   action) as a viewer. **Expect**: `ask_question` and `list_models`, and
   nothing higher-privilege (there is nothing higher-privilege in this
   feature's MVP, but the test suite proves the filtering mechanism itself
   against a synthetic higher-role skill — see `tests/test_skills.py`).
9. Call `list_models`. **Expect**: the same models/dimensions/measures
   catalog `ask_question` is grounded on, including any declared synonyms.
10. Reconnect with no credentials (or an invalid token). **Expect**: the
    connection itself is rejected before any tool is listed or callable —
    no anonymous MCP access.

## Rate limiting

11. Call `ask_question` more than `CI_MCP_RATE_LIMIT_PER_MIN` times within a
    minute. **Expect**: the calls past the limit return
    `outcome: "rate_limited"` immediately (no multi-second LLM round trip),
    and an audit entry (`mcp_skill:ask_question:rate_limited`) is written
    for each rejected call.

## Out of scope — confirm it stays out of scope

12. Confirm no MCP tool exists for saving a visual/dashboard, triggering a
    pipeline run, authoring a model measure, or anything sandbox-agent
    related (`tools/list` never contains them) — this feature is
    read/query-only (spec.md FR-011) and the sandbox coding agent is
    explicitly excluded (FR-010).

## Regression: the existing browser chat is unaffected

13. Use the browser's **Chat** feature exactly as before (spec 012's own
    quickstart). **Expect**: identical behavior — the promoted
    `app/nlq.py` orchestration functions are a rename + move, not a
    behavior change.
