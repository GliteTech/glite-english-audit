# AGENTS.md

Instructions for Codex and compatible coding agents working in this repository. This file governs
how you change the repository itself, not how the audit skills run. CLAUDE.md points here.

## Project purpose

Glite English Audit finds high-confidence non-native English mistakes in English a learner has
naturally written or dictated while using their computer. This repository holds the local side:
discovery, extraction, verification, the local review page, and a schema-valid submission package.
The learner may then contribute that package anonymously to Glite, which returns the report. No
module here calls an inference API; semantic judgments run through the active Codex or Claude Code
runtime and the skills in `skills/`.

## The pipeline shape

Five steps. One session is one file, and that file keeps its name from step a to step e:

```text
runtime/runs/<run-id>/steps/
├── a-collected/       raw user messages                script  pipeline/collect.py
├── b-deduplicated/    duplicates removed               script  pipeline/deduplicate.py
├── c-authored/        non-user text removed            agent   one per file
├── d-mistakes/        privacy-clean mistake records    agent   one per file
└── e-verified/        confidentiality confirmed        agent   one per file
```

Four properties hold, and code that breaks one of them is wrong even if its tests pass:

- **Same names throughout.** A session that produced nothing is an empty file, never a missing
  one. Missing and empty mean different things, and only one of them is what happened.
- **Step c keeps every item.** An utterance that was entirely someone else's text comes back with
  empty text, not deleted, so c's output diffs against its input line by line.
- **Deduplication is global and script-only.** The same dictated sentence pasted into a coding
  agent lands in two sessions with different identifiers by construction, so a per-file pass can
  never see both. The word count is the denominator of every rate the product reports.
- **Step d owes clean records; step e only confirms.** Step e may drop a record, never rewrite
  one, and the system must stay correct if step e is deleted. A scanner hit in step d is a defect
  in step d, not a filter doing its job.

Filenames are opaque sequence numbers (`session-0001.jsonl`) with the mapping in a local
`session-index.json`. `session_hash` is not a path component: it has no validator, two adapters
read it from unvalidated JSON, and a filename reaches the model's context.

**Agents read a projection and write a decision; drivers write the artifacts.** Steps c, d and e
hand each agent what its judgment needs — an index, the text, its modality — and take back
only what it decided. No session hash, path hash or utterance ID reaches a model, which is the
same rule the opaque filenames follow, applied to file contents. The shapes are in
`src/glite_english_audit/pipeline/agent_io.py`; the agent's files live in `steps/<step>/agent/`.

## Critical rules

- Never commit real user transcripts, snapshots, findings, credentials, or private paths.
  All fixtures and examples are synthetic. Secret-looking fixture values must be unmistakably
  fake (e.g. `sk-FAKEFAKEFAKE0000`).
- `temp/` is Git-ignored and private. Nothing in `temp/` may be quoted into committed files.
- Discovery code and scripts must never print, log, or return source text. Agent-facing
  discovery output is `InstanceInventorySummary` only.
- Wrappers under `.claude/skills/` and `.codex/skills/` are generated. Never hand-edit them.
- Follow the specification-first workflow in CONTRIBUTING.md. Non-trivial changes need a
  research finding before code.

## Environment

- Python 3.12+, managed with uv.
- Setup: `uv sync --locked --all-groups`
- Run every command through uv: `uv run <command>`.

## Quality gate

Every change must pass all of these before commit:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python -m glite_english_audit.verification.verify_skills
uv run python -m glite_english_audit.artifacts.schema_export --check
```

`uv run pytest -m "not slow"` deselects the scale test (`tests/test_scale_pipeline.py`) during quick
iterations. The gate above still runs it. `.pre-commit-config.yaml` runs the same checks except the
schema-export check; run that one yourself before commit.

## Layout

```text
skills/                       # canonical agent skills (SKILL.md per skill)
.claude/skills/, .codex/skills/  # generated wrappers — do not edit
src/glite_english_audit/
├── __init__.py               # CLIENT_VERSION
├── paths.py                  # every filesystem location and OS detection
├── sessions.py               # one session is one file: naming, reading, the local index
├── consent.py                # CONSENT_POLICY_VERSION
├── artifacts/                # Pydantic models, envelope, hashing, io, manifest,
│                             # submission contract, schema export
├── diagnostics/              # stable diagnostic code registry
├── state/                    # run/step state machine, run store, event log
├── discovery/                # adapter protocol, registry, snapshot safety,
│                             # scan exclusions, inventory CLI
├── adapters/                 # one package per source: aider, claude_code, cline,
│                             # codex, cursor, gemini_cli, opencode, roo_code,
│                             # wispr_flow (registered in adapters/__init__.py)
├── normalization/            # tokenizer, language spans, authorship filter, dedup
├── pipeline/                 # the step drivers an agent invokes: start_run,
│                             # collect (a), deduplicate (b), authorship (c),
│                             # mistakes (d), verify (e), build_review, save_choice
├── verification/             # deterministic verifiers, corpus/skill verifiers,
│                             # privacy scanner, fixture policy, generate_wrappers
├── progress/                 # progress model and rendering
├── estimation/               # token/time/cost estimation
├── review_server/            # loopback final-review page server and its CLI
└── submission/               # package materializer, capability check, client
schemas/                      # generated JSON Schemas — regenerate, never handwrite
specifications/               # committed specs: agent_skills_specification.md,
                              # compatibility_matrix.md, sources/<adapter_id>.md, ...
styleguide/                   # python, agent-instruction, and prompting style guides
calibration/                  # committed default token-usage profile (numbers only)
fixtures/                     # synthetic fixtures: fixtures/<adapter_id>/<variant>/,
                              # plus fixtures/privacy_adversarial/ for the privacy corpus
tests/                        # pytest; files test_<area>_<topic>.py; use tmp_path
temp/                         # private, ignored development area (research findings, drafts)
runtime/                      # private, ignored runtime data the product writes
```

## Code conventions

- Fully typed; `mypy --strict` clean. `ruff format` and `ruff check` clean (line length 100).
- Pydantic v2 models with `extra="forbid"`. JSON Schemas are generated by
  `schema_export`, never written by hand.
- Enums from `artifacts/enums.py`; compare members, not strings. `None` for missing values.
- Paths and OS locations only via `paths.py`. Never hardcode.
- Diagnostics only via `Diagnostic.from_code(...)`; the code registry is append-only.
- Tests never write into the repository or the real runtime root; use `tmp_path`.

## Skills

- Canonical skills live in `skills/<name>/SKILL.md`. Format rules are in
  `specifications/agent_skills_specification.md`.
- Regenerate wrappers with `uv run python -m glite_english_audit.verification.generate_wrappers`.
  `verify_skills` fails the build on wrapper drift.

## Writing style

Short, direct American English; start with the point. See `styleguide/*.md`. Banned words:
unlock, leverage, seamless, empower, actionable insights, comprehensive analysis, journey,
robust framework, delve into.

## Contributing

CONTRIBUTING.md defines scope, the research gate, and review flow. Material features and new
adapters need an issue plus research/specification review before code.
