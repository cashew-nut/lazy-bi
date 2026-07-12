# Feature Specification: Safe Measure Compilation

**Feature Branch**: `claude/safe-measure-compilation-qhzd5t`

**Created**: 2026-07-12

**Status**: Draft

**Input**: User description: "Replace 'a measure is an arbitrary Python string executed at query time' with 'a measure is a constrained DSL expression compiled to a Polars expression by an allowlisting compiler that never calls eval'. Put trusted model-measure authoring behind auth with provenance. Tier 1 (trust/provenance) and Tier 2 (capability/allowlisting compiler) close the code-injection vulnerability for the common case without a sandbox; Tier 3 (sandboxed execution) is out of scope."

## Clarifications

### Session 2026-07-12

Three forks were surfaced during design that the original brief didn't anticipate (it didn't know about the existing `frame`-based measure, the YAML-vs-SQLite tension with specs 005/007, or that no auth exists at all). All three were put to the maintainer with a recommended option each; the maintainer had no preference on any of them, so the recommended option was taken for each:

- Q: One real model measure (`months_to_75` in `models/clinical_ops_recruitment.yaml`) uses a multi-statement "framed" transform (group-by, window functions, date arithmetic across rows) that no scalar expression DSL can express. Under a strict read of the brief (model and inline measures share one compiler, fail closed on anything the DSL can't express), this measure becomes non-functional the moment the old eval path is deleted, with nothing to catch it until a future sandboxed executor exists. What should happen to it? → A (recommended, taken): Model measures may keep a `frame` escape hatch (today's `compile_frame`, still `eval`/`exec`-based) available **only** behind the new Tier 1 auth+provenance gate; inline/untrusted measures never get this path and always fail closed into the Tier 3 seam. This is a deliberate, explicit amendment to "same compiler, same power for both tiers" for this one construct, justified because a `frame` snippet is only ever accepted from an authenticated model-measure save, not from a query-time request body.
- Q: Where does model-measure provenance (author, version, timestamps) live, given today models are YAML-only and two prior specs (005, 007) assert "no database-backed model store is introduced"? → A (recommended, taken): Hybrid — the model YAML file remains the sole source of truth for the measure's DSL text (Principle I intact, YAML stays hot-reloadable and diffable in git); a new SQLite table records an append-only provenance/history log (one row per save: model, measure name, expression text, author, timestamp, version number) written transactionally alongside the YAML write. The YAML file is never made to carry provenance metadata inline.
- Q: No auth mechanism exists anywhere in the app today. What should gate the new mutating model-measure endpoints? → A (recommended, taken): Add a minimal, pluggable single-shared-secret API-key dependency (a header checked against a configured value), explicitly documented as a placeholder for a real identity system later. The "author" recorded in provenance is a display name/label sent alongside the key (not a verified identity) until a real auth system exists — this is a known limitation, not a claim of strong attribution.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A dashboard viewer's ad-hoc measure can no longer run arbitrary code (Priority: P1)

A Measure Lab / query-builder user types an inline measure expression that gets sent to the server as part of a query. Today that text is `eval`'d in-process with `pl` in scope; a malicious or careless expression can escape into arbitrary Python and touch the filesystem, environment, or process. After this change, the same expression is parsed into an AST and matched against a strict allowlist of columns, operators, and named functions; nothing outside that allowlist can execute, and no code path ever calls `eval`, `exec`, or `compile` on request-supplied text.

**Why this priority**: This is the actual vulnerability the feature exists to close, and it is exploitable today by anyone who can reach the query endpoint (no auth exists at all currently). Closing it does not depend on anything else in this feature.

**Independent Test**: Send each expression in the red-team suite (`__import__('os').system(...)`, `().__class__.__bases__[0].__subclasses__()`, `open('/etc/passwd')`, attribute access, comprehensions, lambdas, I/O calls, UDF calls, unknown functions/columns, oversized/deeply-nested input) as an inline measure and confirm every one is rejected with a clear error and none of them execute — independent of any model-measure or auth work.

**Acceptance Scenarios**:

1. **Given** a query request with an inline measure using only allowlisted constructs (e.g. `sum(revenue) / count_distinct(user_id)`), **When** the query runs, **Then** it returns the correct value, computed via a constructed `polars.Expr`, with no call to `eval`/`exec`/`compile` anywhere in the path.
2. **Given** a query request with an inline measure containing any red-team payload, **When** the query runs, **Then** the request is rejected with a clear compile error identifying what was disallowed, and no code from the payload executes.
3. **Given** an inline measure referencing a column that does not exist on the target model's resolved schema, **When** it is compiled, **Then** it is rejected with a message naming the unknown column.

---

### User Story 2 - Trusted developers author saved model measures with accountability (Priority: P1)

A developer with access to author the semantic layer adds or edits a named, reusable measure on a model (the kind referenced by id from dashboards). Doing so now requires presenting the shared authoring credential; on save, the system records who made the change and bumps a version counter, and the measure is validated through the same safe compiler used for inline measures — a measure that cannot compile is refused at save time, never persisted in a broken state.

**Why this priority**: This is the governance half of the feature and is independently valuable and testable even before any UI exists — it can be exercised entirely through the API. It is P1 because "no auth on mutation" is the other half of today's exposure (anyone who can reach the API can rewrite the semantic layer).

**Independent Test**: Call the model-measure create/update endpoint without credentials and confirm it is refused; call it with credentials and confirm the measure is saved, compiles, and the response/persisted record shows an author label, a timestamp, and an incremented version; call it with an intentionally invalid expression and confirm it is refused and nothing is persisted.

**Acceptance Scenarios**:

1. **Given** no authoring credential is presented, **When** a request tries to create, update, deactivate, or delete a model measure, **Then** the request is rejected (401/403) and no change is made.
2. **Given** a valid authoring credential, **When** a developer saves a new or edited model measure whose expression compiles under the safe DSL, **Then** the measure is persisted, the model's YAML reflects the new expression text, a new provenance/history row is written recording author label, timestamp, and version, and the measure is immediately usable in queries.
3. **Given** a valid authoring credential, **When** a developer submits a model measure expression that fails to compile under the safe DSL, **Then** the save is refused with a clear error and neither the YAML nor the provenance history is modified.
4. **Given** a saved model measure, **When** any user (with no authoring credential) runs a query that references it by name, **Then** the query succeeds — reading/using a saved measure never requires the authoring credential.

---

### User Story 3 - An existing "framed" measure keeps working under the new trust boundary (Priority: P2)

The one existing production measure that needs multi-step, cross-row logic (`months_to_75`, a median time-to-threshold calculation built from a grouped, windowed, re-shaped derived frame) continues to work after this feature ships, because it is defined as a model measure saved through the authenticated authoring path — not because the safe DSL learned to express arbitrary frame reshaping. The same construct submitted as an inline (query-time, unauthenticated) measure is refused.

**Why this priority**: Without this story, shipping the feature as literally specified would silently break a real, working measure with no replacement — a regression a maintainer would want to catch before merge, not discover after. It is P2 because it is a narrower, single-construct carve-out layered on top of Story 2's auth gate, not new capability.

**Independent Test**: Re-save the existing `months_to_75` measure (or an equivalent frame-based measure) through the authenticated model-measure endpoint and confirm it still computes the same result end to end; submit the identical `frame`/`frame_emits` construct as an inline measure on `/query` and confirm it is refused with an error that names the sandboxed-execution seam as the reason (not a generic parse failure).

**Acceptance Scenarios**:

1. **Given** the authenticated model-measure endpoint, **When** a `frame`-bearing measure definition is submitted, **Then** it is accepted, persisted with provenance like any other model measure, and produces correct query results.
2. **Given** an unauthenticated inline measure request, **When** it includes a `frame`/`frame_emits` construct, **Then** it is rejected — this path is never available to inline/query-time measures regardless of credentials.
3. **Given** the existing `months_to_75` measure in `models/clinical_ops_recruitment.yaml`, **When** the feature ships, **Then** it still returns the same values it does today against the same fixture data.

---

### User Story 4 - A measure the safe DSL cannot express fails with an actionable error, not a crash (Priority: P3)

A developer (via the authenticated path) or a query-builder user (inline) writes a measure using a construct that is legitimately outside the scalar DSL's scope (not a security payload, just unsupported — e.g. a string-manipulation helper not yet in the function allowlist). The system tells them clearly that this construct isn't supported by the safe measure language, rather than a stack trace or a silent wrong answer.

**Why this priority**: Good failure UX matters but is lower priority than closing the vulnerability and keeping existing measures working; it's a polish/DX layer on top of Stories 1-3.

**Independent Test**: Submit a measure using an unsupported-but-benign construct (e.g. an unlisted function name) and confirm the error message names the specific unsupported construct and does not resemble a Python traceback.

**Acceptance Scenarios**:

1. **Given** a measure expression using a function name not in the allowlist, **When** it is compiled, **Then** the error explicitly names the unrecognized function and does not execute any part of the expression.
2. **Given** a measure expression that exceeds the size/nesting limits, **When** it is compiled, **Then** it is rejected with a message stating the limit that was exceeded.

---

### Edge Cases

- **Existing model measures using old syntax**: today's 34 measures across `models/*.yaml` are written as Python-method-chain expressions (e.g. `pl.col("total_amount").sum()`), not the new DSL's function-call form (e.g. `sum(total_amount)`). These must be rewritten to the new DSL grammar as part of this feature so every existing dashboard keeps working after the old eval path is deleted — this is not a "no legacy measures" greenfield situation, despite the initial brief's assumption.
- **Inline measure shadowing a model measure of the same name**: both must go through the identical safe compiler; there is no path where an inline measure gets more capability than a model measure (except the frame carve-out in Story 3, which inline measures never get at all).
- **A model measure edited to something that no longer compiles**: the save is refused before anything is written (validate-on-save); the previously-saved, still-valid version remains active and queryable.
- **Provenance history and the YAML file disagreeing** (e.g. YAML hand-edited outside the API): read paths always trust the live YAML for query execution; the provenance table is a history/audit log of saves made through the API, not an alternate source of truth, and is allowed to fall behind hand-edits made outside the API (out of scope to reconcile).
- **API key compromise**: since the placeholder auth is a single shared secret, there is no per-user revocation; rotating the configured key revokes all holders at once. This limitation is documented, not solved, in this feature.
- **Oversized or deeply nested expressions** (either tier): rejected before any construction work begins, with a limit-specific error.
- **Unknown column referenced by a model measure at save time**: refused at save (validate-on-save), same as inline compile-time column validation.

## Requirements *(mandatory)*

### Functional Requirements

**Tier 2 — safe compiler (applies to both inline and model measures)**

- **FR-001**: The system MUST provide a `compile_measure`-equivalent function that parses measure DSL text into an AST (never `eval`/`exec`/`compile`d) and constructs a `polars.Expr` by walking that AST against a strict allowlist.
- **FR-002**: The compiler MUST allow only: constant literals (int/float/str/bool/None), bare-name column references validated against the target dataset's resolved schema, arithmetic (`+ - * / % **`), unary (`+ - not`), comparisons (`== != < <= > >= in not in`), boolean combinators (`and`/`or`), and calls to a fixed set of named functions — and MUST reject every other AST node type (attribute access, subscripting, lambdas, comprehensions, f-strings, starred args, ternaries, await, walrus, assignment) outright.
- **FR-003**: The function allowlist MUST include, at minimum, the aggregations `sum, mean, min, max, count, count_distinct, median, std, var, first, last` and the combinators `col, where, if_, coalesce, cast`.
- **FR-004**: The compiler MUST NOT expose any function that accepts a user-supplied callable, performs I/O (file, network, S3 scan/read/write), or otherwise breaks out of pure column-expression construction.
- **FR-005**: The compiler MUST reject any measure exceeding a configured maximum raw-text length, AST node count, or nesting depth, with an error naming which limit was exceeded.
- **FR-006**: Compilation MUST resolve every bare-name/`col(...)` reference against the target dataset's actual schema at compile time and reject unknown columns with a clear message.
- **FR-007**: Every measure — inline (query-time) and model (saved) — MUST be compiled through this same function; there MUST be no path, for either kind, that executes measure text as Python outside this allowlist, except the narrow, authenticated-only `frame` carve-out in FR-012.

**Execution path integration**

- **FR-008**: The existing in-process `eval`/`exec` path for scalar measure expressions (`compile_expr` and its callers) MUST be removed once the safe compiler is wired in as the sole path for scalar measures; no execution-mode flag or fallback-to-eval configuration MUST exist.
- **FR-009**: The 34 existing measures in `models/*.yaml` MUST be rewritten to the new DSL grammar and MUST continue to produce the same values against the same fixture/benchmark data (taxi, sales, subscriptions, logistics, marketing, and the non-framed measures in clinical-ops-recruitment) after the rewrite.

**Tier 1 — trusted model-measure authoring**

- **FR-010**: Creating, updating, deactivating, and deleting a saved model measure MUST require a valid authoring credential (the new minimal API-key dependency); reading/using a saved measure by name in a query MUST NOT require it.
- **FR-011**: On every authenticated create/update, the system MUST persist an append-only provenance record (model, measure name, expression text, author label, timestamp, version number) in addition to writing the measure's DSL text into the model's YAML file, and MUST refuse to persist either if the expression does not compile (validate-on-save).
- **FR-012**: The authenticated model-measure save path MAY accept a `frame`-based measure definition (today's multi-statement, `exec`-based derived-frame construct) as a distinct, explicitly-flagged capability separate from the Tier 2 allowlist — available only through this authenticated path, never through inline/query-time measures, and never through anything reachable without the authoring credential.
- **FR-013**: The `months_to_75` measure in `models/clinical_ops_recruitment.yaml` MUST be re-saved through the authenticated path (establishing its first provenance record) and MUST continue to return correct values.

**Fail-closed behavior and the Tier 3 seam**

- **FR-014**: When a measure cannot be expressed by the Tier 2 allowlist (and, for inline measures, is not eligible for the FR-012 carve-out), compilation MUST fail closed with an error that is distinguishable from "this is a security violation" versus "this construct just isn't supported yet," and MUST NOT execute any part of the rejected input.
- **FR-015**: The system MUST expose a single, well-named extension point (an executor-selection seam) where a future sandboxed executor could register itself to handle measures Tier 2 rejects; this feature MUST implement only the current "reject with an actionable error" behavior at that seam and MUST NOT implement, import, or depend on any sandbox/process-pool/subprocess execution code.

**Non-functional / compatibility**

- **FR-016**: No functional or performance regression MUST occur for any measure representable in the new DSL — existing dashboards depending on `models/*.yaml` measures must return identical results before and after this feature ships (excluding `months_to_75`, covered by FR-013).
- **FR-017**: All changes MUST stay entirely on files owned by this feature; no file belonging to any sandbox/worker-pool/subprocess-execution branch MUST be read, imported, or modified.

### Key Entities *(include if feature involves data)*

- **Measure (DSL text)**: A small declarative expression over dataset columns (aggregation + arithmetic + a fixed set of helper functions), authored either inline (per-query, unauthenticated) or as part of a model (saved, authenticated). Both compile through the identical Tier 2 allowlist compiler, except the model-only `frame` carve-out.
- **Model measure provenance record**: An append-only history entry (model, measure name, expression text snapshot, author label, timestamp, version number) written on every authenticated create/update of a saved measure. The YAML file remains the executable source of truth; this record is the audit trail.
- **Framed model measure**: The narrow, authenticated-only construct (today's `frame`/`frame_emits`) allowing a saved model measure to define a multi-step derived frame. Not part of the Tier 2 allowlist; not available to inline measures under any circumstance.
- **Authoring credential**: The minimal shared-secret API key gating mutating model-measure endpoints, paired with a self-declared author label used for provenance (not a verified identity).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of a red-team suite of known Python-escape payloads (`__import__`, `__class__`/`__subclasses__` walks, `open`, `getattr`, attribute access, I/O calls, UDF calls, comprehensions, lambdas, subscripts, f-strings, unknown functions, unknown columns, oversized/deep input) are rejected by the compiler with zero code execution.
- **SC-002**: 100% of representative correctness cases (plain aggregate, ratio of aggregates, filtered aggregate, conditional, coalesce, cast, count-distinct) compile and match hand-computed expected values on a fixture dataset.
- **SC-003**: 100% of the 34 existing `models/*.yaml` measures, after rewrite to the new DSL, return values identical to today's `eval`-based results against the same benchmark data.
- **SC-004**: 0 unauthenticated requests can create, update, deactivate, or delete a model measure; every authenticated save produces a provenance record with a strictly increasing version number for that measure.
- **SC-005**: The one existing framed measure (`months_to_75`) continues to return correct results after being re-saved through the authenticated path, and the identical frame construct submitted as an inline measure is refused 100% of the time.
- **SC-006**: No code path anywhere in the scalar-measure execution flow calls `eval`, `exec`, or `compile` on measure input (verified by code inspection / static check, not just tests).

## Assumptions

- The brief's claim that "no legacy Python measures exist" is corrected by this spec: `models/*.yaml` has 34 real measures in the old eval syntax plus one framed measure; both must be carried forward (rewritten or carved out) rather than discarded, per FR-009/FR-012/FR-013.
- The DSL surface described in the brief (aggregations + arithmetic + `where`/`if_`/`coalesce`/`cast`/`col`) is sufficient to express all 33 non-framed existing measures; this has been spot-checked against the actual `expr:` lines in `models/*.yaml` and holds.
- "Author label" recorded in provenance is a user-supplied display string accompanying the shared API key, not a verified per-user identity — acceptable because a stronger identity system is explicitly out of scope for this feature (per the brief) and is flagged as a known limitation rather than silently implied to be strong attribution.
- The provenance history table is additive (new SQLite table) and does not change how model YAML files are loaded, hot-reloaded, or treated as the executable contract (Principle I is preserved for the expression text itself; only the audit trail is new).
- Tier 3 (sandboxed execution) remains entirely out of scope; the extension point is a documented seam only, with no sandbox code added, imported, or referenced.
- Data-access control / row-level security, result caching/materialization, and any change to the S3/Polars scan layer beyond schema-based column validation are out of scope, per the original brief.
