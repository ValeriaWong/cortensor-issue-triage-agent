# Issue Triage Agent — single-issue deep triage + routing

> **One issue, fully routed.** Give it `owner/repo#N` and it reads the
> issue, its comments and linked PRs, then tells you **what it is, how bad
> it is, which component it belongs to, who should own it, and which queue
> it goes to** — with a confidence score and reasons. A draft first reply
> is included. Nothing is ever posted without a human saying yes.

Built for **Cortensor Hackathon #5** with `openminion==0.1.21`. Free tier
only: the default model is Pollinations (no key, $0).

## Why this, not another batch triager

Every other triage entry in this hackathon is a **batch** shallow
classifier: read 20 issues, suggest labels, rank the top 5. None of them
does a single issue in depth, none routes to a person or a queue, and none
reports confidence. This agent's main mode is the opposite:

| | Batch triagers (5 known competitors) | This agent |
|---|---|---|
| Main mode | whole-repo batch | **single-issue deep dive** (`--issue N`) |
| Component / owner | none | **component + suggested assignee** (CODEOWNERS / contributors / history) |
| Confidence + reasons | none | **confidence 0–1 + rationale + evidence quotes** |
| Routing decision | "suggested labels + next step text" | **route enum** (`ready-to-fix`, `needs-info`, `good-first-issue`, `needs-maintainer`, …) |
| Approval gate | 2 of 5 have one | fail-closed snapshot gate, single-use |
| Quantified eval | fixture-only (5/5, 4/6) | **real repo, maintainer labels as ground truth** (see below) |

Batch mode exists (`--limit N`) but is a side feature, not the pitch.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # optional; defaults already point at Pollinations (free)
```

Requirements: Python ≥3.11, `openminion==0.1.21` (pinned), pydantic,
httpx, typer, pytest.

## Quickstart

```bash
# Deep-triage one real issue (2 model calls, ~2 min: paced ≥45 s apart)
issue-triage triage --repo openminion/openminion --issue 778

# Same, from a URL
issue-triage triage --issue-url https://github.com/openminion/openminion/issues/778

# Batch as a side feature (N open issues)
issue-triage triage --repo openminion/openminion --limit 5

# Zero-LLM-cost demo from the bundled fixture
bash scripts/demo.sh
```

`triage` prints the verdict + draft reply and saves a **pending snapshot**
under `.issue-triage/pending/`. It never touches GitHub.

### Approve (the only write path)

```bash
# Execute once: apply labels + post the draft comment (needs GITHUB_TOKEN)
issue-triage approve --snapshot openminion-openminion-778-fbb60bfb --yes

# Or reject: nothing is sent, the snapshot is blocked
issue-triage approve --snapshot openminion-openminion-778-fbb60bfb --no
```

Without `GITHUB_TOKEN`, `--yes` prints the exact `gh api` commands for a
human to run instead of pretending it succeeded. An approved snapshot is
**consumed once** — a second approve is refused (anti-replay).

## Architecture

```
issue-triage triage --repo o/r --issue N
  │
  ├─ GitHub (public reads, no auth needed)
  │   issue + comments + timeline (linked PRs) + repo labels
  │   + CODEOWNERS + top contributors + past decisions for this repo
  │
  ├─ Classifier subagent ── 1 model call ──▶ TriageVerdict (Pydantic)
  │   type / severity / component / labels / assignee / route /
  │   confidence + rationale + evidence. Labels are filtered in code
  │   against the repo's real label list; invented labels are dropped.
  │
  ├─ Drafter subagent ──── 1 model call ──▶ TriageDraft (isolated context:
  │   only the verdict + issue summary, no shared transcript)
  │
  └─ Snapshot (.issue-triage/pending/<repo>-<n>-<hash>.json, pending)
        └─ human approve ─▶ labels + comment posted once (or gh commands)
```

**Why a direct HTTP client instead of SDK `Agent.run`:** one native SDK
turn fires several consecutive model calls; on the Pollinations free tier
(1 reply / 45 s / IP, HTTP 402) the first call consumes the quota and the
rest fail, and the SDK's retry backoff is hardcoded to 300 ms and ignores
`Retry-After` (see `docs/WHAT_WE_LEARNED.md` §5). The direct client makes
exactly one call per step, paces calls ≥45 s apart, honours `Retry-After`,
and parses answers through two channels — `tool_calls` first, plain-text
JSON second — both validated by the same Pydantic model.

**Memory:** past decisions per repo live in `state/<owner>__<repo>.json`
and are handed to the classifier as reference, so labels and routes stay
consistent inside one repo. Export/import uses the SDK `MemoryBundle`
round-trip; note the SDK's public `import_bundle()` persists nothing —
our `RepoMemory.import_bundle_file` does the actual load
(see `docs/WHAT_WE_LEARNED.md` §6).

## Provider presets

| Preset | Endpoint | Key | Status |
|---|---|---|---|
| `openai-compatible` (default) | `https://text.pollinations.ai/openai/v1`, model `openai` (runs `gpt-oss-20b`) | none | works, $0 |
| `cortensor-portal` | `CORTENSOR_PORTAL_BASE_URL` | `CORTENSOR_PORTAL_API_KEY` | preset only — needs official access coordination |
| `cortensor-router` | `CORTENSOR_ROUTER_BASE_URL` | `CORTENSOR_ROUTER_API_KEY` | preset only — needs official access coordination |

Switch with `--preset` or `ISSUE_TRIAGE_PRESET`. Portal/router never
block the default flow. OpenRouter `:free` models are **not** wired in:
they need a key from Allen, and no key is ever invented or stored here.

## Evaluation (real repo, maintainer labels as ground truth)

```bash
issue-triage eval --repo openminion/openminion --limit 10   # ~10 min, 1 call/issue
issue-triage eval --repo openminion/openminion --limit 30   # ~25 min
```

<!-- EVAL-RESULTS: filled from eval/results-*.json after the run -->

Real run, 2026-11-17, `openminion/openminion`, 10 most recent labelled
issues, Pollinations `gpt-oss-20b`, $0 — evidence in
`eval/results-20261116-1739.json` (11.7 min wall clock, 12 model calls:
10 + 2 slim-prompt retries after `output_token_limit_exceeded`):

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| bug | 0.857 | 1.000 | 0.923 | 6 |
| feature | 1.000 | 0.750 | 0.857 | 4 |

Overall **accuracy 0.9 (9/10)**, 0 errors, 0 skipped. The single miss
(#777, `enhancement` predicted as `bug`) came with confidence 0.70 —
the model was correctly unsure.

Confidence calibration: mean confidence 0.839 on correct vs 0.70 on
wrong; predictions with confidence ≥0.8 were 6/6 correct; 0.5–0.8 were
3/4. High confidence means something here; low confidence is the signal
to route to a human.

Run a bigger batch with the same command and a larger `--limit`
(30 issues ≈ 25 min, 50 ≈ 40 min, paced ≥45 s apart).

## Probes

```bash
issue-triage probe                 # full suite, regenerates docs/BUILDING_BLOCKS_TESTED.md
issue-triage probe --probe tool_and_forced_tools
issue-triage probe --offline       # only the no-model probes
```

10 probes, each run for real against the block it names. Statuses are
`pass` / `partial` / `fail` — the SDK-native `Agent.run` probes are
honestly marked `fail`/`partial` with the 402 evidence, not claimed as
passes. See `docs/BUILDING_BLOCKS_TESTED.md` (auto-generated) and
`docs/WHAT_WE_LEARNED.md` (only findings we actually reproduced, with
steps — including two new SDK rough edges: the hardcoded 300 ms retry
backoff and the no-op public `import_bundle()`).

## Tests

```bash
.venv/bin/python -m pytest -q   # 81 tests, loopback HTTP stubs, zero LLM cost
```

Covers CLI parsing, verdict validation (illegal enum values rejected),
the approval state machine (allow / deny / pause-resume / single-use
anti-replay), memory storage + bundle round-trip, GitHub parsing,
the LLM client's dual-channel parsing, pacing and 402 handling, and eval
metrics.

## Files

| Path | What it is |
|---|---|
| `src/issue_triage/` | the package (`cli`, `triage`, `llm_client`, `github_client`, `memory`, `approval`, `eval`, `probe`, `config`, `models`) |
| `tests/` | 81 pytest tests (stubbed, $0) |
| `fixtures/openminion-issue-778.json` | real public issue snapshot (openminion/openminion#778) + repo labels |
| `scripts/demo.sh` | one-command demo on the fixture (2 real model calls, ~2 min) |
| `docs/BUILDING_BLOCKS_TESTED.md` | probe results (auto-generated) |
| `docs/WHAT_WE_LEARNED.md` | reproduced findings only |
| `SPIKE.md`, `probe_*.py` | pre-implementation spike: SDK + Pollinations verification |
| `eval/results-*.json` | eval evidence (kept in the repo) |

## Limitations (honest)

- The free model is `gpt-oss-20b` via Pollinations: JSON structure is
  stable, label semantics drift — hence `Literal` enums + code-enforced
  label filtering. `component` is still a free string (on #778 it returned
  `brain` where the repo label is `area:brain`); normalising it against
  `area:*` labels is future work.
- One issue costs ~2 min (45 s pacing × 2 calls); a 30-issue eval costs
  ~25 min. That is the price of the free tier, not a bug.
- No `GITHUB_TOKEN` in the environment means `approve --yes` cannot
  execute — it prints commands instead. This is by design (fail-closed).
- `eval` ground truth is maintainer labels, which are noisy: an issue can
  carry `bug` + `enhancement` at once (counted as ambiguous and skipped).
