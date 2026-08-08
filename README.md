# Glite English Audit

Find high-confidence non-native English mistakes in the English you naturally wrote or dictated on
your computer. The audit runs locally, through Codex or Claude Code, on text from your coding-agent
and dictation history. It favors precision over recall: it flags only constructions that strongly
suggest non-native English, and it omits anything uncertain.

**Status: early development. Not yet released. Interfaces and contracts may change.**

## What it does

1. Trusted local scripts scan supported applications (Claude Code, Codex, Wispr Flow, and others)
   and build an aggregate inventory: how many messages, words, and sessions exist, and when.
   No message text leaves your machine and none is shown to the model during this scan.
2. You choose sources and a time period, see token/time/cost estimates, and confirm.
3. Only then is the selected text sent — through your own active agent runtime — to your current
   AI provider for analysis. Glite never receives your raw text.
4. Every finding is verified by an independent second pass, turned into a privacy-safe record, and
   checked again by an independent privacy audit.
5. A local review page (loopback only) shows every record exactly as it would be sent. You can
   exclude any record, download the exact submission JSON, or — when a compatible Glite endpoint
   is live — submit anonymously.

## Trust boundaries

- **Local discovery before provider access.** Discovery scripts read source data locally, return
  only aggregate numbers, make no network requests, and never print source text.
- **Honest provider boundary.** Selected text is processed by the AI provider behind your own
  Codex or Claude Code session. The audit is not 100% local, and this README does not claim it is.
- **Glite receives no raw text.** Only privacy-safe mistake records and anonymous counts can enter
  a submission package, and only after your explicit review.
- **Submission is anonymous, permanent, and irrevocable.** Glite asks for no name, email, or
  account. Accepted records are stored permanently; there is no deletion token.
- **Adults only.** Submission requires an explicit 18+ self-attestation.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Codex or Claude Code.

```bash
git clone https://github.com/GliteTech/glite-english-audit
cd glite-english-audit
uv sync --locked --all-groups
```

Then, in Codex or Claude Code, say:

> Run an English audit.

## Development

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python -m glite_english_audit.verification.verify_skills
```

See `CONTRIBUTING.md` for the specification-first workflow, `AGENTS.md` for agent instructions,
and `styleguide/` for the normative Python, agent-instruction, and prompting style guides.

## License

Apache License 2.0. Copyright Glite Tech Ltd. See `LICENSE` and `NOTICE`.
