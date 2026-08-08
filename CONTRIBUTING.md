# Contributing

Thanks for contributing to Glite English Audit. Start with `AGENTS.md` for repository rules and
the quality gate; this file covers scope, workflow, and contribution terms.

## Scope

Welcome without prior discussion:

- Bug fixes.
- Tests and synthetic fixtures.
- Documentation improvements.
- Small improvements to existing adapters.

Require an issue first, plus research and specification review before any code:

- Material features.
- New source adapters.
- Changes to schemas, the submission contract, privacy rules, or consent flow.

Open the issue, agree on the design, and complete the research gate below before writing the
implementation. Pull requests for material work without this step will be closed with a pointer
here.

## Setup

```bash
uv sync --locked --all-groups
```

Run every command through uv (`uv run pytest`, `uv run ruff check .`, and so on).

## Research gate

Non-trivial changes are specification-first. A change is non-trivial when it affects source
access, external APIs or file formats, security, privacy, schemas, dependencies, model or prompt
behavior, billing, quotas, cross-platform compatibility, accessibility, or more than one module.

Before implementation:

1. Research current official documentation, source code, and release notes for the external
   behavior you depend on. Record versions, URLs, and access dates.
2. Record confirmed facts, conflicts, unknowns, and platform differences.
3. Transfer durable decisions into committed specifications or documentation.
4. Propose a concrete fixture and test matrix.

For new adapters this means a reviewed source specification and a full synthetic fixture set
(success, empty, malformed, unsupported-schema, and migration cases) before adapter code lands.

## Tests and style

Every pull request must pass the full quality gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python -m glite_english_audit.verification.verify_skills
uv run python -m glite_english_audit.artifacts.schema_export --check
```

Style rules live in `styleguide/` (Python, agent instructions, prompting) and
`specifications/agent_skills_specification.md`. Skill wrappers under `.claude/skills/` and
`.codex/skills/` are generated; regenerate them with
`uv run python -m glite_english_audit.verification.generate_wrappers` instead of editing.

Tests go under `tests/`, named `test_<area>_<topic>.py`, and write only to `tmp_path` — never to
the repository or the real runtime root.

## Fixtures and privacy

- All fixtures and examples are synthetic. Never include real transcripts, real conversations,
  real file paths containing a person's name, or real credentials.
- Secret-shaped fixture values must be unmistakably fake (e.g. `sk-FAKEFAKEFAKE0000`).
- `temp/` is Git-ignored and private; do not copy its contents into committed files.
- Discovery code must never print, log, or return source text.
- Do not report security vulnerabilities in public issues; see `SECURITY.md`.

## License and contribution terms

This project is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`). By
contributing, you license your contribution under Apache-2.0. V1 does not require a separate
contributor license agreement.
