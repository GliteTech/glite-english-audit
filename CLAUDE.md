# CLAUDE.md

Read `AGENTS.md` and follow it. It is the canonical instruction file for agents working in this
repository.

Quick reference:

- Setup: `uv sync --locked --all-groups`
- Run everything through uv: `uv run <command>`
- Quality gate before any commit:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python -m glite_english_audit.verification.verify_skills
uv run python -m glite_english_audit.artifacts.schema_export --check
```

Never commit real user data. Fixtures are synthetic. `temp/` is private and ignored. Wrappers in
`.claude/skills/` and `.codex/skills/` are generated — never edit them by hand.
