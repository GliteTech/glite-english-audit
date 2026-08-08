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

For new adapters this means a reviewed source specification and a full synthetic fixture set before
adapter code lands. See "Adding a source adapter" below.

## Adding a source adapter

Follow `specifications/adapter_authoring_guide.md`. The order is fixed, and each step gates the
next:

1. **Research specification.** Write `specifications/sources/<adapter_id>.md` and get it reviewed
   before any adapter code or production fixture exists. Cover macOS, Windows, Linux, and WSL
   separately, with storage locations, schema fingerprints, which field is raw versus cleaned, the
   credential and context denylists, known migrations, and the fixture plan. Cite sources with
   access dates.
2. **Fixtures.** Add `fixtures/<adapter_id>/<variant>/`, one directory per variant, each with a
   `fixture.json` whose `kind` is `success`, `empty`, `malformed`, `unsupported`, `migration`, or
   `unit`, and `synthetic: true`. Every adapter ships the first five. All content is synthetic;
   secret-shaped values must carry `FAKE`, `SYNTHETIC`, `EXAMPLE`, or `PLACEHOLDER`.
   `tests/test_verification_fixture_policy.py` enforces this over the whole tree.
3. **Adapter package.** Add `src/glite_english_audit/adapters/<adapter_id>/` exposing `ADAPTER_ID`
   and `create_adapter()`, and implement the `SourceAdapter` protocol from
   `src/glite_english_audit/discovery/base.py`: `discover()`, `snapshot()`, `extract()`, `verify()`,
   plus `adapter_id`, `adapter_version`, and `stability`. Discovery is local-only and returns
   aggregates; unknown fingerprints report `SOURCE_UNSUPPORTED_SCHEMA` instead of guessing.
4. **Registration.** Add the module to `_ADAPTER_MODULES` in
   `src/glite_english_audit/adapters/__init__.py`. Registration is explicit and alphabetical.
5. **Tests.** Add `tests/test_adapter_<adapter_id>.py` covering each fixture variant, the
   role and field allowlists, snapshot safety, and fail-closed schema drift.
6. **Compatibility matrix.** Add the adapter's rows to `specifications/compatibility_matrix.md`
   with the storage variant, per-platform status, and raw-field provenance. Mark a platform
   `verified` only after a real-installation smoke test on that platform; otherwise it stays
   `fixtures-only`. Smoke-test content is never committed.

New adapters start at `beta` or `experimental`. `stable` requires the release gates in the
authoring guide, including a real-installation smoke test for every claimed platform variant.

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

`uv run pytest -m "not slow"` deselects the scale test while you iterate; the gate above still runs
it. `pre-commit` runs the same checks except the schema-export check.

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
