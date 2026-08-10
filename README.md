# Glite English Audit

Find the English mistakes you actually make, from your Claude Code history.

You already write English in Claude Code every day. This reads that history back,
finds the mistakes, and shows you the list. Your messages never go to Glite. Only
the list does, and you see every line first.

## How it works

1. Clone this repo.
2. Run `/audit` in Claude Code.
3. It reads your Claude Code history.
4. It finds the mistakes in your English.
5. You check the list.
6. Send it for a report — the list, not your messages.

## Questions people ask first

**Does Glite see my messages?** No. It receives a list of mistakes — the wrong
construction, the rule, and a short example. You can read every line before you
send it, and exclude any of them.

**Is there a privacy risk in letting Claude Code read this?** No new one. These
are messages you typed into Claude Code; it received them when you wrote them.
Reading them back discloses them to nobody who did not already have them.

**Why only Claude Code?** Because it is enough. Your Claude Code history holds
more natural English than any test would, and reading one source keeps the whole
thing explainable in three sentences. If it turns out you have too little there,
the audit offers to read Codex, Cursor or your dictation history as well.

**What is a good report?** About 200-300 mistakes, which is enough to show a
habit rather than an accident. That is roughly two weeks of ordinary use. The
audit works the period out for you and asks once before it starts.

## Current status

Pre-release. There is no tagged release, and interfaces and contracts may still
change. macOS is the only platform with real-installation smoke tests; every
other platform is covered by synthetic fixtures alone. Direct submission is off
because no Glite submission endpoint exists yet, so the review page is
download-only. `specifications/compatibility_matrix.md` records what has actually
been tested.

## Under the hood

The audit favors precision over recall: it flags only constructions that strongly
suggest non-native English, and omits anything uncertain. Findings are built as
privacy-safe records from the start — the example is your own sentence, your
sentence with an identifying detail replaced, or an invented one, whichever keeps
the most of your words while staying safe to send — and a separate confidentiality
pass checks every record before you see it.

## The five steps

One session is one file, and that file keeps its name through every step, so what any step did to
any session is a line-by-line diff:

```text
runtime/runs/<run-id>/steps/
├── a-collected/       your messages, exactly as the applications stored them   (script)
├── b-deduplicated/    the same text said twice, kept once                      (script)
├── c-authored/        everything you did not write yourself removed            (agent)
├── d-mistakes/        privacy-safe mistake records                             (agent)
└── e-verified/        confidentiality confirmed                                (agent)
```

Steps a and b are ordinary local scripts and never involve a model. Steps c, d and e run one agent
per file, in parallel. Filenames are opaque sequence numbers — `session-0001.jsonl` — so no session
identity reaches the model; the mapping stays in a local index that is never sent anywhere.

The same rule applies inside the files. Each agent is shown only what its judgment needs — the
text, what it was numbered, and whether it was typed or dictated — and hands back what it decided.
The files themselves are written by local scripts.

Those agents run on whatever model your session is running. Nothing here pins a model or offers a
choice of one: this repository makes no inference call. The preflight tells you which model it
observes before you agree to anything, says so plainly when it cannot read one, and says when the
time and token estimates were measured on a different model — which they usually were, since
`calibration/token-usage-profile.json` records what was measured, not what your session will run.

## Supported sources

An audit reads **Claude Code** and nothing else. That is the whole default, and it
is what makes the privacy answer short: those messages were already typed into
Claude Code.

Eight more adapters ship and stay one flag away. The audit offers them only when
your Claude Code history holds too little for a useful report — which is the one
moment reading another application buys you anything.

Stability still governs what may be read at all: stable sources with a supported
schema are eligible, beta sources are not unless you ask.

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

Then, in Claude Code opened on this directory, type:

> /audit

Or in either Claude Code or Codex, just say:

> Run an English audit.

Both start the same procedure: it says what it will read, asks once, picks a
period, and runs. `skills/run-english-audit/SKILL.md` is the whole of it.

## How a run works

**Setup.** One statement and one question: it says it will read your Claude Code
history and that only the mistakes leave the machine, and asks whether it may.
Paths, project names, and workspace names stay on your machine and are never
shown to the model — the model sees text and an index, nothing that identifies
where it came from.

**Period.** The audit picks one, rather than showing you a menu. It aims at
200-300 findings, which is what shows a habit instead of an accident, using the
measured rate of 10.5 findings per 1,000 words you wrote. On ordinary use that
lands around two weeks. You are told which period it chose and what it expects to
find, and changing it is one sentence.

**Resuming.** Everything a run writes stays inside the checkout, under the Git-ignored
`runtime/` directory: one place to inspect and one place to delete, identical on every
platform. Deleting the checkout removes every trace of your audits with it. Say
"Run an English audit" again and the agent offers any unfinished run. The resume decision is
deterministic: matching versions continue from the next session file; changed skills, prompts, or
models recompute findings and every later step; changed adapter, artifact-schema, tokenizer, or
consent versions require a new run. Unfinished runs keep their private artifacts for 30 days after
the last checkpoint.

**Review and submission.** After step e, a loopback review page starts and prints its address. The
page gives each record one compact row showing its privacy-safe submitted example and whether that
example is your own words, your words with a detail changed, or invented; its info button reveals
the complete record. You can include or exclude records,
but not edit them. Excluding a record removes its details and still adds one to the anonymous
withheld count. When direct sending is available, two confirmations start unchecked: that you are
at least 18, and that you accept permanent, irrevocable storage and the disclosed uses. A
download-only page omits those send-only confirmations because the Glite website collects them on
upload. Downloading the package is always available. The download is your only way to retrieve a
report later, so keep it.

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
`AIDER_INPUT_HISTORY_FILE`, `AIDER_CHAT_HISTORY_FILE`, `XDG_DATA_HOME`, or `OPENCODE_DB`. Each
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
`uv run python -m glite_english_audit.review_server --run-id <run-id>`. If it prints "This run has
nothing to review yet," the run has not finished step e.

**A review checkbox will not stay changed.** A saved HTML copy cannot write decisions, and a live
page loses that ability after its local server stops. Restart the command above and open the new
`127.0.0.1` address it prints. Use that live page rather than opening the saved HTML file.

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
