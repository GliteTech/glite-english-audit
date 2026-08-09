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
├── consent.py                # CONSENT_POLICY_VERSION
├── artifacts/                # Pydantic models, envelope, hashing, io, manifest,
│                             # submission contract, schema export
├── diagnostics/              # stable diagnostic code registry
├── state/                    # run/stage state machine, run store, event log
├── discovery/                # adapter protocol, registry, snapshot safety,
│                             # scan exclusions, inventory CLI
├── adapters/                 # one package per source: aider, claude_code, cline,
│                             # codex, cursor, gemini_cli, opencode, roo_code,
│                             # wispr_flow (registered in adapters/__init__.py)
├── normalization/            # tokenizer, language spans, authorship filter, dedup,
│                             # stage-3 filter_corpus CLI
├── pipeline/                 # the stage drivers an agent invokes: start_run, collect,
│                             # authorship_batches, apply_authorship, batches,
│                             # promote_records, build_review, save_choice
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
