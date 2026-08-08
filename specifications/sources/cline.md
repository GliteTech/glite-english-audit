# Cline source specification

Adapter ID: `cline`
Status: source specification for review (spec section 4.2 research gate)
Research log: `temp/findings/cline-source-research.md` (evidence IDs E1–E12 cited below)
Access date for all cited evidence: 2026-08-08

Cline (VS Code marketplace ID `saoudrizwan.claude-dev`, formerly "Claude Dev") is a coding-agent
extension whose vendor now ships the same engine in VS Code, a JetBrains plugin, and a CLI. It has
three on-disk conversation generations. This research is based entirely on the vendor's source
repository (`cline/cline`, current `main` at v4.1.6 plus historical tags v1.0.9, v3.0.0, v3.89.2),
vendor documentation, and maintainer issue threads — no live installation was inspected, so every
platform row below is "supported, untested locally" until the release smoke test runs.

Version timeline researched (E5, E9, E11, strong): task persistence with
`api_conversation_history.json` + `claude_messages.json` (2024, "Claude Dev" era);
`claude_messages.json` renamed to `ui_messages.json` on 2024-10-09 with a read-and-delete
migration still present at v3.0.0 (2024-12); task history index moved from the editor's
`state.vscdb` global state to an on-disk `state/taskHistory.json` around v3.28–v3.31
(2025-09); standalone core (`cline-core`, JetBrains/CLI) defaulting to `<home>/.cline/data/`
by v3.40 (2025-12); SDK session store (`sessions/`) introduced with the SDK-backed extension in
v4.0.0 (2026-06-26), rolled back to the 3.89.2 codebase in v4.0.1 (2026-06-28), re-landed and
current in v4.1.x (2026-07/08).

V1 requirement (project spec 4.7): support versioned per-task API history names; API history is
preferred over UI history. Cline and Roo Code share heritage, but this specification never assumes
identity with the `roo_code` adapter; every rule below carries Cline-specific evidence.

## 1. Platform status and storage locations

Cline has two families of storage roots. Never infer one platform's path from another.

Family A — editor `globalStorage` (VS Code hosts; conversation generations G1/G2, section 3):

| Platform | Status | Default root (VS Code stable) | Evidence |
| --- | --- | --- | --- |
| macOS | Supported, untested locally | `<home>/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/` | E9 (moderate, vendor-confirmed issue text) |
| Windows (native) | Supported, untested locally | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\` (Roaming, not Local) | E9 (moderate) |
| Linux (native) | Supported, untested locally | `<home>/.config/Code/User/globalStorage/saoudrizwan.claude-dev/` | E9 (moderate) |
| WSL (VS Code Remote-WSL) | Supported, untested locally | `<wsl-home>/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev/` | E9 (moderate, user-reported path listing) |

Family B — Cline data directory (standalone core, CLI, JetBrains ≥ ~3.40, and the v4 SDK
extension; conversation generations G2/G3):

| Platform | Status | Default root | Evidence |
| --- | --- | --- | --- |
| macOS | Supported, untested locally | `<home>/.cline/data/` | E2, E6, E7 (strong, vendor code + docs) |
| Windows (native) | Supported, untested locally | `%USERPROFILE%\.cline\data\` | E2, E8 (strong for resolution code; path itself untested) |
| Linux (native) | Supported, untested locally | `<home>/.cline/data/` | E2, E7 (strong) |
| WSL | Supported for the WSL-side store | `<wsl-home>/.cline/data/` | E2 (code is platform-neutral; WSL variant untested) |

Details:

- Editor variants replace the `Code` path component of family A: `Code - Insiders`,
  `VSCodium`, `Code - OSS`, `Cursor`, `Windsurf`, and code-server at
  `<home>/.local/share/code-server/User/globalStorage/saoudrizwan.claude-dev/`. Discovery
  enumerates a fixed list of known editor data roots per platform; each existing root is a
  separate source instance. Unknown editors are never guessed (platform convention, moderate; only
  the `Code` roots are vendor-confirmed for Cline, E9).
- The current v4 VS Code extension still passes the editor's `globalStorage` path into its host
  provider (`context.globalStorageUri.fsPath`, E12, strong), so legacy task directories remain
  under family A even on v4 installs, while new v4 conversations are written to family B
  `sessions/` (E1, E8, strong).
- Family B resolution order (single source of truth in vendor code, E2, strong):
  `CLINE_DATA_DIR` env var; else `CLINE_DIR` env var + `/data`; else `<home>/.cline/data`.
  Discovery honors both env vars when set. Home resolution on Windows prefers `HOME`, then
  `USERPROFILE`, then `HOMEDRIVE`+`HOMEPATH` (E8, strong).
- An additional observed variant nests family B inside family A:
  `<globalStorage-root>/data/tasks/…` (standalone core hosted by an IDE with its config pointed
  at the extension directory; observed in a maintainer thread, E9, weak). Discovery probes
  `<root>/tasks/` and `<root>/data/tasks/` under every family A root; each layout found is the
  same G2 schema.
- JetBrains: older plugin builds used
  `%APPDATA%\JetBrains\<IDE>\globalStorage\saoudrizwan.claude-dev\` (E9, weak-to-moderate;
  vendor later stated this path is no longer used). Current JetBrains uses family B. The old
  JetBrains variant is not discovered in V1; see unresolved question 6.
- WSL: a Cline CLI or Remote-WSL extension inside WSL writes to the WSL home (family A
  `.vscode-server` variant, family B `~/.cline`). A native Windows store is reachable from WSL
  only via `/mnt/<drive>/Users/<user>/…`; it is plain JSON files but an untested variant. V1 does
  not auto-discover it: when such a mount path exists, discovery emits a hint diagnostic
  directing the user to run the audit from native Windows (project spec 1.3).
- Retention: no automatic cleanup period exists upstream; tasks persist until deleted from the
  history UI. Long, multi-year histories are normal (E9).
- All conversation files are plain JSON documents (arrays or objects), rewritten whole on save.
  There is no JSONL, no compression, no encryption in the conversation stores. SQLite exists in
  the stores only as denylisted index/infrastructure databases (section 2), which the adapter
  never opens, so the project's database snapshot rules (spec 4.6) are satisfied by never
  touching a database at all.

### Layout under a family A root (G1/G2; E1, E3, E4, strong)

```text
saoudrizwan.claude-dev/
├── tasks/<taskId>/
│   ├── api_conversation_history.json   ← extraction source (allowlisted)
│   ├── ui_messages.json                ← structure-only (timestamps, cross-check)
│   ├── claude_messages.json            ← pre-2024-10-09 name of ui_messages.json (G1)
│   ├── context_history.json            ← context-window bookkeeping; never opened
│   ├── task_metadata.json              ← file-context tracking; never opened
│   └── settings.json                   ← per-task model settings; never opened
├── state/taskHistory.json              ← history index (≥ ~3.28–3.31); never opened
├── settings/cline_mcp_settings.json    ← never opened
├── cache/                              ← never opened
├── checkpoints/<cwdHash>/.git          ← workspace shadow git repos; never opened
└── data/                               ← optional nested family B store (probe tasks/ only)
```

`<taskId>` is an epoch-milliseconds string (`Date.now().toString()`, E3, strong). Discovery
treats it as opaque for identity but may use it as a task-start timestamp fallback (section 5).

### Layout under a family B root (G2 + G3; E1, E2, E8, strong)

```text
<home>/.cline/data/
├── tasks/<taskId>/…                    ← same G2 layout and files as family A
├── sessions/
│   ├── sessions.index.json             ← session index incl. prompt text; never opened
│   ├── subagent-spawn-queue.json       ← never opened
│   └── <sessionId>/
│       ├── <sessionId>.json            ← session manifest (eligibility + timestamps only)
│       ├── <sessionId>.messages.json   ← extraction source (allowlisted)
│       ├── *.messages.json             ← other agents' artifacts; eligibility-gated
│       └── hooks.jsonl                 ← observability telemetry; never opened
├── state/taskHistory.json              ← never opened
├── globalState.json                    ← never opened
├── secrets.json                        ← never opened (API keys, mode 0600)
├── settings/                           ← providers.json, global-settings.json,
│                                          cline_mcp_settings.json; never opened
├── db/                                 ← sessions.db, connectors.db, cron.db, locks.db; never opened
├── cache/, workspaces/, teams/, connectors/, logs/   ← never opened
```

## 2. Files that must never be opened

The adapter opens only, per instance: `tasks/<taskId>/api_conversation_history.json` (extraction),
`tasks/<taskId>/ui_messages.json` or its G1 name `claude_messages.json` (structure-only), and per
session directory `sessions/<sessionId>/<sessionId>.json` (eligibility/timestamps) and
`sessions/<sessionId>/*.messages.json` (extraction, eligibility-gated). Everything else is
denylisted. Explicitly forbidden:

- `secrets.json` (family B root) — provider API keys and tokens (E2, strong).
- `globalState.json`, `state/taskHistory.json`, `sessions/sessions.index.json` — contain
  task/prompt text snippets and workspace paths duplicating the conversation files, with no
  structure the extractor needs (E1, E2, E8).
- `settings/` in either family — `cline_mcp_settings.json` (MCP server commands, env credentials),
  `providers.json`, `global-settings.json` (E1, E2, E8).
- `db/` — `sessions.db`, `connectors.db`, `cron.db`, `locks.db`; SQLite infrastructure. Never
  opened, never snapshotted; the messages files carry all conversation content (E8, strong).
- `checkpoints/` — shadow Git repositories containing full workspace file snapshots
  (`checkpoints/<cwdHash>/.git`, E3, strong). Highest-risk content in the store; never entered.
- `tasks/<taskId>/context_history.json`, `task_metadata.json`, `settings.json` — context-window
  bookkeeping, workspace file-path tracking, and model configuration (E1, E2).
- `sessions/<sessionId>/hooks.jsonl` and `subagent-spawn-queue.json` (E8).
- `cache/` (both families), `remote_config_<orgId>.json` files (E1).
- `workspaces/`, `teams/`, `connectors/`, `logs/` under family B (E7, E8).
- The editor's own files outside the extension directory: `state.vscdb` / `state.vscdb-*`
  (pre-3.28 `taskHistory` global state and the encrypted VS Code SecretStorage rows holding
  Cline's provider keys), `settings.json`, keychain items, and every sibling extension
  directory. The adapter never opens any editor database.
- `<home>/Documents/Cline/` (Rules, Workflows, Hooks, MCP) and workspace-side `.clinerules`,
  `.cline/`, `.clineignore` — configuration, not conversation (E1).

Rationale: user-authored conversation text is fully contained in the two allowlisted
conversation artifacts. Everything else adds credential, workspace-content, or path exposure
with no extraction value.

## 3. Record schema by generation

No conversation file carries a schema version except G3 (`version: 1`). The adapter fingerprints
files by shape and never branches on the application version.

### 3.1 G1/G2 `tasks/<taskId>/api_conversation_history.json` (E3, E4, E6, strong)

A single JSON array. Each element is an Anthropic `MessageParam`: `role: "user" | "assistant"`,
`content` either a string or an array of blocks (`text`, `image`, `tool_use`, `tool_result`).
Unlike Roo Code, Cline's API records carry no `ts` and no summary/condense flags — timestamps
come from section 5, and generated summaries are structurally excluded because they are not
inside user-text wrappers.

User-text carriers inside user-role records (constant from v1.x through v3.89.2; vendor code
comments state that all user-generated content is placed in exactly these tags, E3, E4, strong):

- Initial task: a `text` block (or string content) containing `<task>\n…\n</task>`.
- Tool denial / completion feedback / stuck-loop guidance: `<feedback>\n…\n</feedback>` embedded
  in scaffold prose inside `text` blocks or `tool_result` content.
- Followup-question answers: `<answer>\n…\n</answer>` embedded in `tool_result` content.
- Resume and plan-mode instructions: `<user_message>\n…\n</user_message>` inside a
  `[TASK RESUMPTION]` or "New instructions for task continuation"/"New message to respond to"
  text block.

Injected, never user-authored (E3, strong): `<environment_details>…</environment_details>`
(appended as its own text block); mention expansions appended after the user's text —
`<file_content path="…">`, `<folder_content path="…">`, `<url_content url="…">`,
`<workspace_diagnostics>`, terminal/git expansions; slash-command expansions
`<explicit_instructions type="…">…</explicit_instructions>` (types include `new_task`,
`condense_response`, and others) spliced into wrapper content; `[TASK RESUMPTION]` boilerplate;
compaction instructions appended when the context window compacts; file attachments serialized
by `processFilesIntoText`; hook-injected context. Mention tokens inside the user's own sentence
are rewritten by upstream to `'<path-or-url>' (see below for file content)` (also
`… site content)`, `… folder content)`, and similar), which makes affected spans `cleaned`
(section 4.3).

### 3.2 G1 variant boundary

Before 2024-10-09 the UI stream was named `claude_messages.json`; versions after the rename
migrate the file on next open (read then delete, still present at v3.0.0; E4, E5, strong). The
API history file has carried the same name `api_conversation_history.json` across all observed
generations. A task directory may therefore contain `claude_messages.json` instead of
`ui_messages.json`; both parse as the same `ClineMessage[]` shape. Claude-Dev-era builds from
mid-2024 predating task persistence leave nothing on disk (E4: v1.0.9 has no persistence code).

### 3.3 G1/G2 `ui_messages.json` / `claude_messages.json` (E3, E4, strong)

Array of `ClineMessage` objects: `ts` (epoch ms), `type: "say" | "ask"`, `say`/`ask` enum,
`text`, `reasoning`, `images`, `files`, `partial`, plus bookkeeping fields. User-authored text
appears as `say: "task"` (the raw initial prompt, unwrapped) and `say: "user_feedback"` /
`"user_feedback_diff"`. Everything else (`api_req_started` request snapshots, `text` assistant
output, tool/command records) is generated. Per the V1 requirement, this channel is never an
extraction source: the adapter reads it structure-only for `ts` values and the section 6.4
cross-check.

### 3.4 G3 `sessions/<sessionId>/<sessionId>.messages.json` (E8, strong)

Versioned vendor contract ("messages.json contract — v1"). File shape:

```jsonc
{
  "version": 1,
  "updated_at": "<ISO-8601>",
  "agent": "lead" | "subagent" | "teammate",
  "sessionId": "<session-id>",
  "taskType": "…",            // optional; subagent/team runs
  "messages": [ … ],
  "system_prompt": "…"        // optional
}
```

Each message: `id` (stable), `role: "user" | "assistant"`, `content` (always an array of
Anthropic-native blocks: `text`, `thinking`, `tool_use`, `tool_result`), optional `ts` (epoch
ms; guaranteed on assistant turn messages only), assistant-only `modelInfo` and `metrics`.
Tool results are `tool_result` blocks on user-role messages — a user-role record is not
sufficient evidence of human text.

User text in G3 is wrapped at persistence time (E10, strong):

- `<user_input mode="act|plan|yolo">…</user_input>` — the user's typed prompt (mentions are NOT
  rewritten in G3; attachments travel out-of-band).
- `<user_command slash="…">…</user_command>` — slash-command envelope; content is command
  arguments, not prose.
- `<mode_notice>…</mode_notice>` — runtime-generated, prepended inside the same text; never
  user text.
- `<file_content path="…">…</file_content>` — attachment blocks; never user text.
- Runtime-composed `<user_input>` bodies also exist: teammate/async run updates beginning
  `System-delivered teammate async run updates:` are model/orchestrator-composed (E10, strong;
  see unresolved question 2).

### 3.5 G3 `sessions/<sessionId>/<sessionId>.json` manifest (E8, strong)

Zod-validated object: `version: 1`, `session_id`, `source`, `pid`, `started_at`, `ended_at?`,
`status`, `interactive`, `provider`, `model`, `cwd`, `workspace_root`, `team_name?`,
`enable_*` booleans, `prompt?`, `metadata?`, `messages_path?`, `compaction_path?`. The adapter
reads only `session_id`, `started_at`, `ended_at`, `status`, and `interactive`; `prompt`,
`cwd`, `workspace_root`, and `metadata` are private content parsed past but never returned,
logged, or hashed into output.

## 4. Inclusion and exclusion rules

### 4.1 Unit eligibility (whole task directory or session directory)

G1/G2 task directory: exclude entirely when `api_conversation_history.json` is missing,
unparseable, or not a JSON array (fallback: a directory with only a parseable UI stream is
counted `ui_only_task` and contributes no text in V1 — API history preferred, and UI-only
directories indicate corruption). Cline's `HistoryItem` has no parent/subtask fields, so no
whole-directory delegation exclusion exists; model-composed initial tasks are handled by the
`new_task` mitigation in 4.2.

G3 session directory: exclude entirely when any holds:

1. No `*.messages.json` parses as the section 3.4 shape with `version == 1`.
2. The payload's `agent` is present and not `"lead"` (subagent and teammate sessions are
   agent-driven; their "user" messages are orchestrator-composed).
3. The manifest exists and `interactive` is `false` (non-interactive automation runs; prompts
   are programmatic).

Within an eligible session directory, extract only from the file named
`<sessionId>.messages.json` whose payload `sessionId` matches the directory name and whose
`agent` is `"lead"` or absent. Other `*.messages.json` files in the directory are excluded and
counted `non_lead_messages_file`.

### 4.2 Candidate user text — G1/G2 (per record, API history only)

A record contributes candidate text only if all hold:

1. `role == "user"`.
2. Text is taken exclusively from recognized user-text carriers: the spans inside
   `<task>…</task>`, `<feedback>…</feedback>`, `<answer>…</answer>`, and
   `<user_message>…</user_message>` in `text` blocks, string content, or `tool_result` content
   (string form or `text` parts of array form). Text outside these wrappers — scaffold prose,
   `[TASK RESUMPTION]` boilerplate, `<environment_details>` blocks, mention-injected
   file/site/folder contents, compaction instructions — is never extracted.
3. `<explicit_instructions …>…</explicit_instructions>` spans inside a wrapper span are
   stripped before extraction (slash-command expansions); the remaining user-typed text is kept
   if non-empty.
4. The extracted span is non-empty after trimming.

`new_task` mitigation (deterministic, within one instance): collect the `context`/`message`
argument of every `new_task` `tool_use` block across all task files; an initial `<task>` span
exactly matching a collected argument is excluded as `subtask_initial_message` (the text was
model-composed for a spawned task). Residual risk is unresolved question 1.

Exclusions (never contribute text): assistant records entirely; `image` and `tool_use` blocks;
`tool_result` content without a wrapper span; whole blocks starting with
`<environment_details>`, `<file_content`, `<folder_content`, `<url_content`,
`<workspace_diagnostics>`; any unknown block type. Wrapper integrity: an opening wrapper tag
without its closing tag excludes the whole block and increments `unbalanced_wrapper`. Unknown
leading tags on otherwise-kept spans keep the text but set
`content_flags: ["unknown_wrapper"]` for normalization quarantine.

### 4.3 Candidate user text — G3 (per message)

A message contributes candidate text only if all hold:

1. `role == "user"`.
2. The message has no `tool_result` block (tool-result carriers are generated traffic; observed
   G3 messages do not mix tool results with typed prompts, and a mixed message is excluded
   fail-closed and counted `mixed_user_message`).
3. Text is taken from `text` blocks as follows: strip `<mode_notice>…</mode_notice>` spans;
   then, if a `<user_input …>…</user_input>` span exists, the candidate is that span's inner
   text; a bare text block with no known wrapper is kept whole (older or host-composed
   messages) but flagged `content_flags: ["unwrapped_user_text"]`.
4. `<user_command …>` envelopes are excluded entirely (command syntax; matches the project's
   Claude Code slash-command precedent; V1 drops the argument text, precision over recall).
5. Candidates whose trimmed text starts with a known runtime-composed prefix are excluded:
   `System-delivered teammate async run updates:` (list extensible; unresolved question 2).
6. Blocks of `<file_content …>` and any other known injected tag are excluded.
7. The result is non-empty after trimming.

### 4.4 Provenance and text status

- G1/G2 wrapper spans contain what the user submitted, unmodified — except @-mention rewriting:
  upstream persists the text after mention expansion, which replaces `@path`/`@url` tokens with
  a quoted path plus a marker phrase. Rule: if an extracted span matches the marker pattern
  `(see below for … content)` or contains `<file_content`/`<url_content`/`<folder_content`,
  the utterance gets `text_status: "cleaned"` and is quarantined per project spec 4.4;
  otherwise `text_status: "verbatim"`.
- G3 `<user_input>` spans are persisted without mention rewriting (E10): `verbatim` after the
  4.3 stripping rules; `unwrapped_user_text` candidates are `unknown` and quarantined.
- Cline does not rewrite, spellcheck, or "enhance" typed prompts before persisting in any
  researched generation (no enhance-prompt feature shipped, unlike Roo Code) (E3, E8,
  moderate — absence of code paths).
- Modality: `written` for all records (dictation into the editor is invisible here; cross-source
  dedup against a voice source is the shared normalizer's job, project spec 4.8/5.5).
- Authorship basis: `explicit_user_role+wrapper` (G1/G2), `explicit_user_role+user_input`
  (G3). Authorship confidence 0.9; 0.7 for G3 `unwrapped_user_text`; 0.6 with
  `possible_delegated_task` for G1/G2 initial messages when the 4.2 `new_task` scan could not
  run (single-file inventory).

## 5. Sessions, timestamps, and deduplication

- Session identity: the task directory name (G1/G2 `taskId`, epoch-ms string) or session
  directory name (G3 `sessionId`). Reported only as a salted local hash. One directory = one
  session.
- G1/G2 timestamps: API records carry none. Fallback order per utterance: the `ts` of the
  matching `ui_messages.json` record (structure-only join: `say:"task"` for the initial task,
  `say:"user_feedback"` in file order for subsequent spans), else the task start (`taskId`
  parsed as epoch ms when it parses cleanly), else file mtime. Unresolvable timestamps make the
  utterance `undated` (excluded from bounded period filters, never treated as zero).
- G3 timestamps: message `ts` when present; else the manifest `started_at`; else file mtime.
- Duplication within one G1/G2 task: resumes append to the same array; compaction adds
  assistant summaries (never extracted). The `<task>` span also appears in `ui_messages.json`
  (`say:"task"`) and in the denylisted history indexes; only the API copy is extracted.
- Duplication within one G3 session: the manifest `prompt` duplicates the first user message
  (never extracted); `compaction_path` artifacts are never read.
- G2 → G3 transition: v4 reads legacy tasks in place and does not migrate them into
  `sessions/`, so the same conversation does not normally exist in both stores. Resuming a
  legacy task under v4 creates an SDK session that may re-embed prior turns; cross-file
  exact-hash dedup (same normalized text; earliest timestamp wins) collapses these. See
  unresolved question 3.
- `new_task`-spawned tasks duplicate the parent's `tool_use` argument (both sides excluded by
  4.2).
- Across instances: VS Code stable/Insiders/VSCodium, family A vs family B, and WSL-side vs
  Windows-host stores are distinct instances with distinct histories in the normal case;
  exact-hash plus temporal dedup in the shared normalizer collapses genuine copies. Roo Code
  began as a Cline fork, but its stores live under a different extension ID with no shared
  files; cross-adapter dedup is text-hash-based only.
- Cross-source dedup (dictated in a voice tool, pasted into Cline) is the shared normalizer's
  job (project spec 4.8); this adapter supplies text hashes and timestamps only.

## 6. Discovery, snapshot, extraction, verification

### 6.1 discover()

1. Build the candidate root list: family A = each platform-appropriate editor data root ×
   `saoudrizwan.claude-dev`, probing `tasks/` and `data/tasks/`; family B = resolved Cline data
   dir (`CLINE_DATA_DIR`, `CLINE_DIR`/data, `<home>/.cline/data`), probing `tasks/` and
   `sessions/`. In WSL, only WSL-home roots; emit the mounted-Windows hint diagnostic when
   `/mnt/<drive>/Users/*/` stores exist.
2. Each existing root is one source instance (a root containing both `tasks/` and `sessions/`
   is still one instance with two layouts). Root exists but has no task/session directories:
   state `found`, zero records.
3. Enumerate `tasks/*/` and `sessions/*/` (skip index files and lock files). For each unit apply
   section 4: stream-parse the allowlisted files, accumulate candidate message/word/byte counts
   and min/max timestamps. Large-file guard: conversation files above a configured ceiling
   (default 256 MB; unbounded histories are a known upstream failure mode, E9) are counted
   `oversized_file` and skipped, never loaded whole.
4. Return only `InstanceInventorySummary` (opaque label such as `Cline 1`): never paths, editor
   names beyond the coarse OS field, task IDs, workspace names, or text.
5. Schema fingerprint per instance: set of observed layouts and generations
   (`g1-claude-messages`, `g2-task-store`, `g3-sdk-sessions`, `mixed`), presence of
   `state/taskHistory.json`, presence of a `db/` directory.

### 6.2 snapshot()

Plain JSON files only; no database is ever opened, so no WAL handling applies. Snapshot = byte
copy of each selected unit's allowlisted files into
`<repository>/temp/runtime/<run-id>/snapshots/cline/<instance-hash>/<unit-hash>/`, after the
project snapshot-path preflight (containment, symlink, git-ignore, synced-root checks; spec
3.6). Producers rewrite these files whole (G3 via temp-file-plus-rename, G1/G2 via plain
rewrite, E3, E8); a copy taken mid-rewrite can be truncated or invalid JSON — the snapshot
reader treats an unparseable snapshot copy as `malformed_file` for that unit (retry once during
snapshotting, then fail that unit closed). Snapshot manifest records source sizes, SHA-256
hashes, and mtimes. Live files are opened read-only; nothing under any source root is ever
written, locked, renamed, or deleted.

### 6.3 extract()

Runs only against the snapshot. Emits `NormalizedUtterance` records (project spec 4.4) with
modality `written`, text status per 4.4, session hash per section 5, and a source-path hash of
the canonical original path (never the path itself).

### 6.4 verify()

Adapter-specific deterministic checks: every utterance maps to a snapshot record passing
section 4; no utterance text contains `<environment_details`, `<file_content`,
`<explicit_instructions`, `<user_input`, `<mode_notice`, or `</task>`-family tags (wrapper
stripping succeeded); no denylisted file appears in the opened-path audit log; G1/G2 units:
every extracted utterance's text appears in the API history file, and when a UI stream was
readable the initial-task cross-check holds (the `say:"task"` text equals the `<task>` span
after whitespace normalization; mismatch raises non-fatal `channel_mismatch` and keeps the API
copy); counts are internally consistent; dedup produced no orphan canonical references.

## 7. Failure behavior

- Conversation file unparseable, not an array/object of the expected shape, or non-UTF-8:
  `malformed_file`; that unit contributes no text; other units unaffected.
- G3 payload with `version != 1`: `detected, unsupported schema` for that unit (vendor promises
  a version bump on breaking change, E8); no guessing.
- Unknown block types, unknown message keys, unknown manifest keys: tolerated and ignored
  (additive-change tolerance is vendor-documented for G3, E8); the containing record is still
  processed by the section 4 rules.
- A `user`-role record whose content is neither string nor array: excluded, counted
  `unrecognized_user_record`; if such records dominate a file (>10% of records), the file falls
  to `unsupported_schema`.
- Instance where every unit is unsupported or malformed: instance state `detected, unsupported
  schema` with diagnostic code; not selectable; no candidate counts.
- Root present but unreadable (permissions): instance state `inaccessible` with diagnostic
  code; never retried with elevated privileges.
- Env override (`CLINE_DATA_DIR`/`CLINE_DIR`) pointing at a missing directory: family B
  reported `not_found`; the default location is NOT probed as a fallback (mirrors vendor
  resolution, E2).
- The adapter never guesses: any shape not matching sections 3–4 yields exclusion plus a
  countable diagnostic, not a heuristic parse.

## 8. Unresolved questions and required behavior when evidence is insufficient

1. G1/G2 `new_task`-spawned tasks: the spawned `<task>` text is model-composed, detected only
   by the 4.2 argument-matching scan, which fails across instances or when the parent task was
   deleted. Behavior: the scan plus `possible_delegated_task` flagging; the shared
   normalization layer remains the second defense. Refresh if the vendor adds a parent-task
   marker.
2. G3 runtime-composed `<user_input>` bodies: only the teammate-update prefix is confirmed.
   Unknown other producers (future notification types) would be extracted as user text.
   Behavior: prefix denylist, `unwrapped_user_text` quarantine for unwrapped candidates, and a
   research refresh before the release freeze against the then-current SDK source.
3. Legacy task resumed under v4: whether the resulting SDK session re-embeds legacy user turns
   in its `messages.json` is unverified (`mergeLegacyUiMessagesWithResumedSdkMessages`
   exists upstream, E1). Behavior: cross-file exact-hash dedup; verify on a real dual-store
   installation during the smoke test.
4. Windows-native paths for both families and the encoded behaviors of `%APPDATA%` variants
   are code-derived but untested. Behavior: discovery enumerates whatever exists; the Windows
   platform gate requires a fixture check before release.
5. Editor variants beyond VS Code stable (Insiders, VSCodium, Cursor, Windsurf, code-server)
   are inferred from the VS Code platform convention, not Cline-specific evidence. Behavior:
   fixed probe list; any store found is processed by the same shape rules; a variant that fails
   shape checks reports `unsupported_schema`, never a guess.
6. Old JetBrains `%APPDATA%\JetBrains\<IDE>\globalStorage\…` stores (pre-standalone-core):
   evidence is a single maintainer thread (E9, weak). Behavior: not discovered in V1; a hint
   diagnostic when the path exists; revisit if JetBrains users are in scope.
7. G3 session IDs generated per run may collide with nothing today; multiple `*.messages.json`
   files in one session directory are only partially understood (subagent artifacts observed in
   code, E8). Behavior: extract only the name-matching lead file (4.1); count others.
8. The exact release that introduced `state/taskHistory.json` (~3.28–3.31) is unpinned.
   No adapter behavior depends on it (the file is never opened); recorded for the
   compatibility matrix only.
9. Whether any 4.0.x transition build wrote G2 tasks and G3 sessions concurrently for the same
   conversation is unverified. Behavior: layout probing plus dedup; smoke-test check.

## 9. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/cline/<variant>/` with a `fixture.json` metadata file. All content
is synthetic; secret-looking values are unmistakably fake (`sk-FAKEFAKEFAKE0000`).

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-g2-vscode` | Family A layout; task with `<task>` block, `<environment_details>` block, mention expansion (`<file_content>` + `(see below for file content)` marker), `<feedback>` in a tool_result, `<answer>`, `[TASK RESUMPTION]` + `<user_message>`, `<explicit_instructions type="new_task">` inside a wrapper span, assistant records, images; matching `ui_messages.json` with `say:"task"`, `say:"user_feedback"` ts values | exact kept-span set; wrapper and instruction stripping; cleaned-status on mention marker; ts join; UI never extracted |
| `success-g1-claude-messages` | Task dir with `api_conversation_history.json` + `claude_messages.json` (no `ui_messages.json`) | G1 fingerprint; identical extraction; legacy UI name read structure-only |
| `success-g2-standalone` | Family B `tasks/` layout under a fake `<home>/.cline/data` with `globalState.json`, `secrets.json`, `settings/`, `db/` present | same extraction; denylist audit proves sibling files unopened |
| `success-g3-sdk` | `sessions/<id>/` with manifest, lead `messages.json` (`<user_input mode="plan">` with `<mode_notice>` prefix, `<user_command slash="team">`, tool_result carrier message, assistant messages with metrics), `hooks.jsonl`, `sessions.index.json` | user_input spans only; mode_notice stripped; user_command and tool_result carriers excluded; index/hooks unopened |
| `g3-subagent-teammate` | Session dirs with `agent:"subagent"`, `agent:"teammate"`, a lead dir containing an extra non-matching `*.messages.json`, and a manifest with `interactive:false` | whole-directory exclusions; `non_lead_messages_file` counting |
| `g3-system-composed` | Lead session whose second `<user_input>` starts with `System-delivered teammate async run updates:` | prefix exclusion |
| `migration-newtask` | Parent task with a `new_task` `tool_use` argument plus spawned task whose `<task>` matches it | `subtask_initial_message` exclusion both scans |
| `empty` | Roots with empty `tasks/`, empty `sessions/`, missing roots | found-empty vs not-found states |
| `malformed` | Truncated mid-rewrite JSON, non-array API file, non-UTF-8 bytes, G3 `version: 2` payload | `malformed_file` / `unsupported_schema` per unit; instance survives |
| `unsupported` | Task dir with only `ui_messages.json`; records with unrecognized user shapes above threshold | `ui_only_task`; fail-closed thresholding |
| `dual-root-wsl` | Fake `.vscode-server` family A root + `~/.cline` family B root + `/mnt/c/Users/<fake>` decoy | two instances, no merge, hint diagnostic for the mount |
| `env-override` | Populated store reachable only via `CLINE_DATA_DIR` | override honored; default root not probed |
| `denylist` | Roots seeded with fake `secrets.json`, `cline_mcp_settings.json`, `sessions.db`, `checkpoints/<hash>/.git`, `state/taskHistory.json` | opened-path audit proves none were read |

Platform matrix: the full pytest suite runs on macOS, Linux, WSL, and native Windows gates.
Path-resolution tests cover both families per platform, `%APPDATA%` vs `%USERPROFILE%`
distinctions, and permission-error handling. A private smoke test against at least one real
installation per claimed platform/storage variant is required before stable release; smoke-test
content and outputs are never committed.

## 10. Reproducible read-only inspection commands

Structure-only commands used for this research; safe to rerun on a real store. Placeholder
paths only. They print names, field keys, types, and counts — never values.

```bash
# Family roots (names only)
ls "<home>/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev" 2>/dev/null
ls "<home>/.cline/data" 2>/dev/null

# Task-unit inventory (counts only)
ls "<root>/tasks" | wc -l; ls "<root>/sessions" 2>/dev/null | wc -l

# G2: role / content-shape census of one task (no text printed)
python3 -c 'import json,sys,collections
c=collections.Counter()
for m in json.load(open(sys.argv[1])):
    ct=m.get("content"); shape=type(ct).__name__
    if isinstance(ct,list): shape+=":"+",".join(sorted({b.get("type","?") for b in ct}))
    c[(m.get("role"),shape)]+=1
print(c.most_common())' "<root>/tasks/<taskId>/api_conversation_history.json"

# G2: wrapper-marker census (booleans only)
python3 -c 'import json,sys
tags=["<task>","<feedback>","<answer>","<user_message>","<environment_details>","<explicit_instructions"]
for m in json.load(open(sys.argv[1])):
    if m.get("role")!="user": continue
    blocks=m["content"] if isinstance(m.get("content"),list) else [{"type":"text","text":m.get("content","")}]
    for b in blocks:
        t=b.get("text") or (b.get("content") if isinstance(b.get("content"),str) else "")
        if isinstance(t,str) and t: print([t.strip().startswith(x) or x in t[:200] for x in tags])' \
  "<root>/tasks/<taskId>/api_conversation_history.json" | sort | uniq -c

# G3: payload envelope keys only
python3 -c 'import json,sys
o=json.load(open(sys.argv[1]))
print(o.get("version"), o.get("agent"), sorted(o.keys()))
import collections; c=collections.Counter()
for m in o.get("messages",[]):
    c[(m.get("role"),",".join(sorted({b.get("type","?") for b in m.get("content",[])})))]+=1
print(c.most_common())' "<root>/sessions/<sessionId>/<sessionId>.messages.json"
```

Never run `cat`, `jq .`, or any command that prints message values against a real store.
