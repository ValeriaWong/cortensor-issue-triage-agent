# What We Learned

Only findings we actually reproduced while building this agent, each with
reproduction steps and evidence. Nothing here repeats upstream issues
`openminion/openminion#773` / `#774` / `#704`, the Verifier's findings, or
WV629's F1–F13 — those belong to their authors.

## 1. `create_isolated_runtime` does not exist in openminion 0.1.21

- **Reproduce:** `getattr(openminion, "create_isolated_runtime")` raises
  `AttributeError`; a full-package grep finds zero definitions.
- **Impact:** the reference work's helper of that name is its own wrapper,
  not SDK API. The working equivalent is
  `APIRuntime.from_config(config, home_root, data_root)` (verified: runtime
  builds in ~1.7 s).
- **Source:** SPIKE.md §2.2; re-confirmed by `probe_runtime_isolation`
  (builds a real isolated runtime in temp dirs).

## 2. The default config cannot start a runtime

- **Reproduce:** `OpenMinionConfig()` then `APIRuntime.from_config(...)`
  raises `ConfigValidationError: OpenMinionConfig.agents is empty`.
- **Impact:** the minimal runnable config must hand-fill one
  `AgentProfileConfig`; the docs don't make that obvious.
- **Source:** SPIKE.md §2.2.

## 3. An empty channel is rejected even in `warn` mode

- **Reproduce:** build the runtime without `default_channel` on the agent
  profile, run a turn: `RuntimeError: inbound authenticity denied
  (reason=invalid_channel, mode=warn)`.
- **Impact:** the error names the symptom, not the fix (`default_channel`
  must be `"console"`). The `warn` label is misleading — the turn is
  refused unconditionally for an empty channel.
- **Source:** SPIKE.md §2.2, first end-to-end attempt.

## 4. A native SDK turn cannot survive the Pollinations free tier (HTTP 402)

- **Reproduce:** `Agent.run` against `https://text.pollinations.ai/openai/v1`
  with `model="openai"`, three attempts on 2026-11-17, all fail in the
  decide phase with `ProviderError: ... HTTP 402: Free limit reached:
  1 reply every 45 seconds per IP. Retry after 19s ...`. A single direct
  HTTP call in the same window returns 200 in ~2.3 s.
- **Why:** one SDK turn fires several consecutive model calls; the first
  consumes the quota and the rest hit 402 immediately.
- **And when it doesn't 402:** probe runs 4 and 5 (2026-11-16 18:07 and
  18:15 UTC) — the native turn survived the rate limit (144–162 s turns)
  and still failed both times: the model replied in prose starting with a
  `triage: ` prefix (`"triage: Sure, I want to ...ue #12 belongs to?"`,
  then `"triage: Issue #12 is a b...ing, JavaScript error)."`) instead of
  the requested `TriageVerdict` JSON, and `Agent.run` raised
  `AgentOutputValidationError`. So the native path is unreliable in two
  different ways on the free tier, not one. (Recorded as a run observation
  from our own probe runs; not claimed as a new SDK bug.)
- **Impact:** the production path is the direct HTTP client (one call per
  step, paced ≥45 s). The probe suite re-attempts one native turn per full
  run and records the outcome honestly instead of claiming a pass.
- **Source:** SPIKE.md §4; `probe_tool_calls` (SDK half).

## 5. Retry backoff is hardcoded to 300 ms and ignores `Retry-After` (new)

- **Reproduce (runtime, no grep):** call
  `openminion.modules.llm.providers.factory._runtime_llmctl_config(...)`
  with any provider payload — the returned config always contains
  `"retries": {"max_retries": 2, "backoff_ms": 300}`. The same literal
  `300` is hardcoded in `modules/llm/providers/bridge.py`. `AgentProfileConfig`
  (the public config surface) has no `retries` field, so no user-facing knob
  reaches this value. Inspecting `modules/llm/runtime/flow.py::_call_with_retries`
  at runtime shows the sleep is `(backoff_ms / 1000) * 2**attempt` and the
  function never reads a `Retry-After` header.
- **Numbers:** default `max_retries=2` gives waits of 0.3 s + 0.6 s = 0.9 s
  total — against a 45 s free-tier window. Even raising `max_retries` cannot
  help, because the base is 300 ms and the provider's own `Retry after 19s`
  is discarded.
- **Impact:** this is the structural reason finding 4 cannot be fixed by
  configuration: the SDK's retry machinery is unusable against any
  rate limit longer than ~1 s unless the code is patched.
- **Source:** probe `error_and_timeout_contracts` (runs the config builder
  and the source check live; also asserts our own client honours
  `Retry-After` on a stubbed 402).

## 6. The public `import_bundle()` persists nothing (new)

- **Reproduce:** `export_bundle([...])` → `save_bundle` → `load_bundle` →
  `import_bundle(bundle, trust_mode="direct")` returns
  `{"trust_mode": "direct", "item_count": 1, ...}`. The function takes no
  store or runtime argument and writes nowhere — reading its source shows
  it only validates `trust_mode` and builds the summary dict. No item is
  loaded into any memory.
- **Impact:** a caller following the documented round-trip gets a summary
  and zero items actually in memory. "Portability" at this layer is
  file-level only; the real load path is the internal
  `MemoryService.import_bundle_snapshot`. Our `RepoMemory.import_bundle_file`
  implements the missing half (validated by a round-trip pytest).
- **Note:** this differs from the Verifier's finding 6 (the file round-trip
  does work); the gap is specifically that the public import does not load
  anything into memory.
- **Source:** probe `memory_bundle_portability`; `tests/test_memory.py`.

## 7. Sandbox `NO_PROXY` breaks httpx worse than SPIKE recorded

- **Reproduce:** the sandbox `NO_PROXY` contains bare IPv6
  `fd8b:4f84:7d32:99::1` and bracketed `[fd8b:4f84:7d32:99::1]` in addition
  to the `[::1]` SPIKE.md §3 recorded. Constructing any `httpx.Client`
  raises `InvalidURL: Invalid port: '4f84:7d32:99::1]'` before any request.
- **Fix:** `config.fix_proxy_env()` now keeps only plain hostname / IPv4
  entries (optionally with a port) and drops everything containing `::` or
  brackets. Reproduced as 2 pytest failures before the fix, 0 after.
- **Source:** `tests/test_cli_config.py::test_fix_proxy_env_strips_ipv6_loopback`;
  `src/issue_triage/config.py`.

## 8. With tools offered, the free model may still answer in plain-text JSON

- **Reproduce:** live triage of `openminion/openminion#778` (2026-11-17,
  Pollinations `gpt-oss-20b`): the classify call was sent with a
  `submit_triage_verdict` tool *and* `response_format=json_object`, and the
  model answered with text JSON (`channel=text`, 23,464 ms). The draft call
  in the same run used `tool_calls` (18,115 ms).
- **Impact:** tool_calls cannot be the only parse path; text-JSON parsing
  is a production path, not a fallback. Both channels go through the same
  Pydantic model in `DirectClient.chat_typed`.
- **Source:** live run log, `eval`/`triage` run 2026-11-17 01:19 UTC;
  snapshot `.issue-triage/pending/openminion-openminion-778-fbb60bfb.json`.

## 9. Free-model label invention is real; enforce the vocabulary in code

- **Reproduce:** SPIKE.md §3.3 — with `json_object` requested, the model
  returned `"label": "failure"`, a value never in the candidate list.
  Structure was valid JSON; the semantics drifted.
- **Fix in this agent:** label-like fields are Pydantic `Literal` enums
  (an invented value fails validation and triggers one correction retry),
  and `suggested_labels` are filtered against the repo's real label list
  in code (`validate_labels` drops invented labels and reports them).
  Live evidence: on #778 the model suggested exactly the repo's real
  labels (`area:brain`, `bug`, `status:maintainer-investigation`).
- **Known gap:** `component` is a free string, not an enum — the same run
  returned `component="brain"` where the repo's label is `area:brain`.
  Component normalisation against `area:*` labels is future work.
- **Source:** SPIKE.md §3.3; `src/issue_triage/models.py`,
  `src/issue_triage/triage.py::validate_labels`; pytest `test_models.py`.

## 12. The free tier can 400 with an upstream constrained-decoding glitch — and cache the 400

- **Reproduce:** probe run 1, 2026-11-17 17:44 UTC, draft call (tools +
  `response_format=json_object`): HTTP 400 `{"code": "BAD_REQUEST",
  "message": "400 Bad Request: unexpected tokens remaining in message
  header: Some(\"<|constrain|>commentary\")"}` (upstream host
  `portkey.myceli.ai`). Probe run 2, five minutes later, hit the same
  draft call twice (17:49:13 and 17:49:58, 45 s apart — the client's
  pacing): **both 400s carried a byte-identical body with the original
  timestamp `17:44:28.306Z`**. The upstream replays the cached error for
  the same request for at least 5 minutes, so re-sending the identical
  constrained request cannot recover.
- **Not the trigger:** an A/B test of the same draft prompt with and
  without `reasoning_effort="low"` (2026-11-17 ~17:53 UTC) passed both
  ways — the 400 is a transient upstream state, not caused by the
  reasoning-effort flag. By then the cached error had expired.
- **Fix in this agent:** `draft()` retries once with a *different request
  shape* (plain text, no tools, no `json_object`) — a fresh cache key that
  bypasses the constrained-decoding path. The probe runner records a
  per-probe failure instead of aborting the whole suite (and still writes
  `BUILDING_BLOCKS_TESTED.md`).
- **Source:** `/tmp/probe_full.log` (run 1), `/tmp/probe_full2.log` (run 2,
  two 400s at 17:49:13/17:49:58, same body timestamp), `scripts/ab_reasoning_effort.py`;
  `src/issue_triage/triage.py::draft`, `src/issue_triage/probe.py::run_probes`.

## 11. The free model can die with 400 `output_token_limit_exceeded` mid-run

- **Reproduce:** first eval run (2026-11-17 17:25 UTC), issue #778 classify
  call: HTTP 400 `{"code": "output_token_limit_exceeded", "message": "The
  model exhausted its output token limit before producing an answer.
  Increase the token limit or reduce reasoning effort."}` (upstream host
  `portkey.myce...`). The same prompt had succeeded 6 minutes earlier in
  the triage run — the failure is nondeterministic: the reasoning model
  sometimes spends its whole output budget on chain-of-thought and emits
  nothing.
- **Fix in this agent:** (a) every call now sends `reasoning_effort="low"`
  by default (overridable via `ISSUE_TRIAGE_REASONING_EFFORT`, empty =
  omit); (b) `classify` retries once on a slimmed prompt (short body, no
  comments/history/CODEOWNERS) after a provider error; (c) `run_eval`
  records a per-issue error and continues instead of aborting the run.
  All three are covered by `tests/test_eval_resilience.py`.
- **Source:** `/tmp/eval10.log` (first attempt); `src/issue_triage/llm_client.py`,
  `src/issue_triage/triage.py::classify`, `src/issue_triage/eval.py::run_eval`.

## 10. One model call per step is what makes the free tier usable at all

- **Reproduce:** the direct client paces calls ≥45 s apart and makes
  exactly one call per step (classify, then draft). The #778 end-to-end run
  took 86.6 s total for 2 calls (23.5 s + 18.1 s model time + 45 s pacing)
  with zero 402s. The 10-issue eval (10 calls) ran clean at the same pacing.
- **Corollary:** token accounting on Pollinations carries ~300 prompt tokens
  of fixed overhead even for tiny prompts (SPIKE.md §3.1: 333 prompt tokens
  for a ~30-token prompt) — budget by the metered number, not the prompt.
- **Source:** live triage + eval runs, 2026-11-17; `eval/results-*.json`.
