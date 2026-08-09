# Glite English Audit

Find high-confidence non-native English mistakes in the English you naturally wrote or dictated on
your computer. The audit runs through Codex or Claude Code, on text from your coding-agent and
dictation history. It favors precision over recall: it flags only constructions that strongly
suggest non-native English, and it omits anything uncertain.

## Current status

Pre-release. There is no tagged release, and interfaces and contracts may still change. macOS is the
only platform with real-installation smoke tests; every other platform is covered by synthetic
fixtures alone. Direct submission is off because no Glite submission endpoint exists yet, so the
review page is download-only. `specifications/compatibility_matrix.md` records what has actually
been tested, per adapter and per platform.

## What it does

1. Trusted local scripts scan supported applications and build an aggregate inventory: how many
   messages, words, and sessions exist, and when. No message text leaves your machine and none is
   shown to the model during this scan.
2. You choose sources and a time period, see token, time, and cost estimates, and confirm.
3. Only then is the selected text sent — through your own active agent runtime — to your current
   AI provider for analysis. Glite never receives your raw text.
4. Every finding is verified by an independent second pass, turned into a privacy-safe record, and
   checked again by an independent privacy audit.
5. A local review page (loopback only) shows every record exactly as it would be sent. You can
   exclude any record and download the exact submission JSON. When a compatible Glite endpoint is
   configured, the page can also submit anonymously.

## Supported sources

Nine adapters ship today. Stability drives default selection: stable sources with a supported
schema are selected by default, beta sources are not.

Four of the nine are beta: Aider, Gemini CLI, Cline, and Roo Code. An adapter is stable only once
its user experience has been observed on a real installation, not merely once its tests pass.
Those four are implemented and tested against synthetic fixtures, but nobody has yet watched one
run against a real install.

Cursor was the fifth until its provenance was measured: on a real store, 81.2% of composer bubbles
are verbatim-equivalent to what the user typed, and the rest are marked as reconciled rather than
guessed. Storage generations other than the one known to keep the prompt exactly as typed are
still inventoried rather than analyzed.

| Source | Adapter ID | Stability | Notes |
|---|---|---|---|
| Claude Code | `claude_code` | stable | Project JSONL transcripts; human-authored user messages only |
| OpenAI Codex CLI | `codex` | stable | Rollout JSONL sessions, including archived ones |
| Aider | `aider` | beta | Input history, with chat Markdown as fallback |
| Google Gemini CLI | `gemini_cli` | beta | Session chats; Windows-host stores fail closed under WSL |
| OpenCode | `opencode` | stable | SQLite store plus the older JSON storage generations |
| Cline | `cline` | beta | Per-task API history |
| Roo Code | `roo_code` | beta | Per-task API history |
| Wispr Flow | `wispr_flow` | stable | Dictation. Only the raw ASR text is read; formatted, edited, clipboard, and context columns are never ingested. Native Windows is required; WSL fails closed. |
| Cursor | `cursor` | stable | Each stored prompt is reconciled against the editor state before it counts as yours; file-mention tokens are stripped. |

Platform status, tested application versions, storage fingerprints, and raw-field provenance are in
`specifications/compatibility_matrix.md`. The evidence behind each adapter is in
`specifications/sources/<adapter_id>.md`.

Check the list on your own machine:

```bash
uv run python -c "from glite_english_audit import adapters; from glite_english_audit.discovery.registry import adapter_ids, create_adapter; adapters.register_all(); [print(a, create_adapter(a).adapter_version, create_adapter(a).stability.value) for a in adapter_ids()]"
```

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Codex or Claude Code.

```bash
git clone https://github.com/GliteTech/glite-english-audit
cd glite-english-audit
uv sync --locked --all-groups
```

Then, in Codex or Claude Code opened on this directory, say:

> Run an English audit.

The agent follows `skills/run-english-audit/SKILL.md` from there: consent, discovery, selection,
preflight, processing, and the final local review.

## How a run works

**Source selection.** Discovery shows one row per detected instance with an opaque label such as
"Claude Code 1", a date range, and candidate counts. Paths, project names, and workspace names stay
on your machine and are never shown to the model. Stable sources with a supported schema and
eligible provenance are selected by default; beta, inaccessible, and unsupported-schema sources are
not. You then pick a period and see an estimate before anything is sent to a provider.

**Resuming.** Everything a run writes stays inside the checkout, under the Git-ignored
`runtime/` directory: one place to inspect and one place to delete, identical on every
platform. Deleting the checkout removes every trace of your audits with it. Say
"Run an English audit" again and the agent offers any unfinished run. The resume decision is
deterministic: matching versions continue from the next utterance; changed skills, prompts, or
models recompute findings and later stages; changed adapter, artifact-schema, tokenizer, or consent
versions require a new run. Unfinished runs keep their private artifacts for 30 days after the last
checkpoint.

**Review and submission.** The last stage starts a loopback review page and prints its address. The
page shows every record exactly as it would be sent; you can include or exclude records, but not
edit them. Excluding a record removes its details and still adds one to the anonymous withheld
count. Two confirmations start unchecked: that you are at least 18, and that you accept permanent,
irrevocable storage and the disclosed uses. Downloading the package is always available. The
download is your only way to retrieve a report later, so keep it.

## Trust boundaries

- **Local discovery before provider access.** Discovery scripts read source data locally, return
  only aggregate numbers, make no network requests, and never print source text.
- **Honest provider boundary.** Selected text is processed by the AI provider behind your own
  Codex or Claude Code session. The audit is not 100% local, and this README does not claim it is.
  No module in this package calls an inference API itself.
- **Glite receives no raw text.** Only privacy-safe mistake records and anonymous counts can enter
  a submission package, and only after your explicit review.
- **The review page is loopback only.** It binds to `127.0.0.1`, lives under a per-run random token
  path, requires that token in a header for every write, rejects cross-origin requests, and shuts
  down after 30 minutes without activity.
- **Submission is anonymous, permanent, and irrevocable.** Glite asks for no name, email, or
  account. Accepted records are stored permanently; there is no deletion token.
- **Adults only.** Submission requires an explicit 18+ self-attestation.

## What V1 does not do

- No accounts, login, or identity linking. There is no way to delete a submitted record later.
- No CEFR level, general English score, or comparison with other learners.
- No shared mistake taxonomy and no cross-user category frequencies. Records are plain and
  taxonomy-free.
- No promise of full recall. The audit deliberately misses uncertain cases to avoid false ones.
- No raw message upload to Glite, and no attempt to bypass any application's encryption.
- No application-level encryption of resumable private artifacts; V1 relies on your operating
  system account and disk security.
- No localization. All user-facing text is English.
- No default selection of beta sources. The four unobserved adapters are offered, never chosen
  for you.

See `FUTURE.md` for what a later version may add.

## Troubleshooting

**Source not found** (`SOURCE_NOT_FOUND`, informational). The application or its data directory is
not on this machine. If your data lives outside the default location, set the documented override
before the run: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `CLINE_DATA_DIR`, `GEMINI_CLI_HOME`,
`AIDER_INPUT_HISTORY_FILE`, `AIDER_CHAT_HISTORY_FILE`, `XDG_DATA_HOME` or `OPENCODE_DB`. Each
source specification lists the overrides it honors.

**Detected, unsupported schema** (`SOURCE_UNSUPPORTED_SCHEMA`). The store exists but its fingerprint
is not one the adapter has tested, usually after an application update. The adapter fails closed and
extracts nothing rather than guessing. Open an issue with the adapter ID and the app version.

**Source database is locked** (`SOURCE_LOCKED`). A SQLite-backed source, such as Wispr Flow, could
not be copied consistently while the application held it. Quit that application and run the audit
again; the adapter never reads a live database in place.

**Snapshot refused because the repository sits in a cloud-synced folder**
(`SOURCE_SNAPSHOT_SYNCED_ROOT`). Snapshots are written under
`<repository>/runtime/runs/<run-id>/snapshots/`. If any parent directory is Dropbox, OneDrive,
Google Drive, iCloud Drive, Box, CloudStorage, or Syncthing, snapshotting stops before a byte of
source data is read. Clone the repository to a local, non-synced path. Two related refusals:
`SOURCE_SNAPSHOT_NOT_IGNORED` (Git does not ignore `runtime/`) and `SOURCE_SNAPSHOT_UNSAFE_PATH`
(a symlink or a target outside the repository).

**The run cannot resume** (`STATE_EXPIRED_INPUT`). An unfinished run whose last checkpoint is more
than 30 days old expires; its private artifacts are deleted on the next launch and only the manifest
is kept. Start a new audit. Resume is also refused when adapter, artifact-schema, tokenizer, or
consent versions changed since the checkpoint.

**The review page will not open.** The address has the form `http://127.0.0.1:<port>/t/<token>/`;
the token is part of the path, and any other path returns 404. Open it on the same machine, since
the server binds only to loopback. It stops after 30 minutes without a request; restart it with
`uv run python -m glite_english_audit.review_server --run-id <run-id>`. If it prints "No reviewed
submission artifact exists for this run yet," the run has not reached the review stage.

**The page says direct sending is not available.** This is download-only mode, and it is the normal
state today. It appears when no `submission-endpoint.json` is present in the runtime `config`
directory, when that file is invalid, when it advertises no compatible package version, or when its
base URL is not `https://`. Download the package and upload it later on the Glite website. Every
diagnostic code above is stable and documented in `specifications/diagnostic_codes.md`.

## Development

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv run python -m glite_english_audit.verification.verify_skills
uv run python -m glite_english_audit.artifacts.schema_export --check
```

`uv run pytest -m "not slow"` skips the scale test during quick iterations; the full gate runs it.

See `CONTRIBUTING.md` for the specification-first workflow, `AGENTS.md` for agent instructions,
and `styleguide/` for the normative Python, agent-instruction, and prompting style guides.

## License

Apache License 2.0. Copyright Glite Tech Ltd. See `LICENSE` and `NOTICE`.
