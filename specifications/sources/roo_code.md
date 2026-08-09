# Roo Code source specification

Adapter ID: `roo_code`
Status: source specification for review (spec section 4.2 research gate)
Research log: `temp/findings/roo_code-source-research.md` (evidence IDs E1–E9 cited below)
Access date for all cited evidence: 2026-08-08

Roo Code is a VS Code extension (marketplace ID `RooVeterinaryInc.roo-cline`), forked from Cline
(then "Roo Cline") in late 2024 and renamed Roo Code at 3.0. It stores each conversation as a
per-task directory of JSON files inside the host editor's `globalStorage`. This research is based
entirely on the vendor's source repository (final commit and historical release tags), its
changelog, and maintainer issue threads — no live installation was inspected, so every platform
row below is "supported, untested locally" until the release smoke test runs.

Upstream status: the vendor announced sunset on 2026-04-20 and shut the product down on
2026-05-15; the GitHub repository is archived read-only and the final extension release is
3.53.0 (E1, E9, strong). The storage format is therefore frozen for this extension ID. The
community continuation fork ("ZooCode", published 2026-05-16 as 3.54.0) is a separate extension
ID and a separate future adapter; it is out of scope here except for the deduplication caveat in
section 8.

Divergence from the Cline adapter is documented explicitly in sections 3–5; identity with the
Cline specification is never assumed. Same V1 requirement as Cline (project spec 4.7): versioned
per-task API history file names; API history preferred over UI history.

## 1. Platform status and storage locations

Roo Code has no storage root of its own. It writes under the host editor's per-user
`globalStorage` directory, so the root depends on operating system × editor variant × extension
variant. Never infer one platform's path from another.

| Platform | Status | Default root (VS Code stable) | Evidence |
| --- | --- | --- | --- |
| macOS | Supported, untested locally | `<home>/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/` | E5 (moderate) |
| Windows (native) | Supported, untested locally | `%APPDATA%\Code\User\globalStorage\rooveterinaryinc.roo-cline\` (Roaming, not Local) | E5 (moderate) |
| Linux (native) | Supported, untested locally | `<home>/.config/Code/User/globalStorage/rooveterinaryinc.roo-cline/` | E5, E6 (moderate) |
| WSL (VS Code Remote-WSL) | Supported, untested locally | `<wsl-home>/.vscode-server/data/User/globalStorage/rooveterinaryinc.roo-cline/` | E5, E6 (moderate) |

Details:

- Editor variants replace the `Code` path component: `Code - Insiders`, `VSCodium`, `Cursor`,
  `Windsurf`, `Antigravity` (observed in a maintainer issue, E5), and code-server at
  `<home>/.local/share/code-server/User/globalStorage/rooveterinaryinc.roo-cline/`. Discovery
  enumerates a fixed list of known editor data roots per platform; each existing root is a
  separate source instance. Unknown editors are not guessed.
- Extension variants: the nightly build uses a different extension directory,
  `rooveterinaryinc.roo-code-nightly`, with the same internal layout (E1, strong: the nightly
  build overrides the package name via `PKG_NAME`). Both directory names are discovered; each is
  a separate instance.
- WSL: when VS Code runs with the Remote-WSL server, the extension host runs inside WSL and the
  store lives in the WSL home (`.vscode-server`, and `.vscode-server-insiders` for Insiders). A
  native Windows VS Code without Remote-WSL keeps its store under `%APPDATA%` on the Windows
  side. Both are plain JSON files; V1 running in WSL discovers the WSL-side `.vscode-server`
  store. The Windows-host store mounted via `/mnt/<drive>` is technically readable flat files,
  but it is an untested variant: V1 does not auto-discover it and reports a hint diagnostic when
  `/mnt/<drive>/Users/<user>/AppData/Roaming/*/User/globalStorage/rooveterinaryinc.roo-cline`
  exists, directing the user to run the audit from native Windows (project spec 1.3).
- Custom storage path: the setting `roo-cline.customStoragePath` (namespace
  `roo-code-nightly.customStoragePath` for the nightly) relocates the `tasks/`, `settings/`, and
  `cache/` subtrees to an arbitrary directory (E1, strong: `src/utils/storage.ts`
  `getStorageBasePath()`). The value lives in the editor's `settings.json`, which V1 never
  opens (section 2). Consequence: a relocated store is not auto-discovered; V1 accepts only an
  explicit user-provided override path in the run configuration. See unresolved question 1.
- Retention: no automatic cleanup period exists upstream; tasks persist until the user deletes
  them from the history UI. Long histories are normal. `safeWriteJson` writes have caused
  transient `*.lock` companion files (proper-lockfile) in bug reports (E5); lock files are
  ignored by discovery.
- The product is discontinued: stores stop growing after 2026-05-15 unless the user installed
  the nightly/fork lineage. An old, static date range is normal, not an error.

### Layout under the extension storage root (E1, strong)

```text
rooveterinaryinc.roo-cline/
├── tasks/<taskId>/
│   ├── api_conversation_history.json   ← extraction source (allowlisted)
│   ├── ui_messages.json                ← never extracted; structure-only cross-check
│   ├── history_item.json               ← eligibility metadata (≥ ~3.50, allowlisted)
│   ├── task_metadata.json              ← file-context tracking; never opened
│   └── checkpoints/                    ← per-task shadow git repo; never opened
├── tasks/_index.json                   ← task-history index cache (≥ ~3.50, allowlisted)
├── settings/                           ← never opened (section 2)
├── cache/                              ← never opened
└── checkpoints/<workspace-hash>/       ← shadow git repos; never opened
```

`<taskId>` directory names changed generation: epoch-milliseconds strings (Roo Cline 2.x, Cline
heritage), then UUIDv4 (3.x), then UUIDv7 (late 3.x) (E1, E2, strong). Discovery treats
directory names as opaque and never parses them for dates.

## 2. Files that must never be opened

The adapter opens only, per instance root: `tasks/_index.json`, and per task directory
`api_conversation_history.json`, `history_item.json`, and (structure-only, for timestamps and
cross-checks; never for extraction text) `ui_messages.json`. Everything else is denylisted:

- `settings/mcp_settings.json` — MCP server definitions may embed command lines, tokens, and
  environment credentials (E1).
- `settings/custom_modes.yaml` and legacy `settings/custom_modes.json` — user prompt config;
  no conversational text; may reveal project context.
- `cache/` — model metadata caches.
- `checkpoints/` at root and `tasks/<taskId>/checkpoints/` — shadow Git repositories containing
  full workspace file snapshots (E1: `ShadowCheckpointService`). Highest-risk content in the
  store; never entered.
- `tasks/<taskId>/task_metadata.json` — `FileContextTracker` records of workspace file paths
  (E1). No user prose; pure path leakage.
- `tasks/<taskId>/claude_messages.json` — vestigial legacy name checked by upstream code (E1);
  under this extension ID it practically never exists (the fallback predates the fork). If seen,
  it is counted `legacy_unmigrated_file` and not parsed (unresolved question 5).
- The editor's own files outside the extension directory: `state.vscdb` / `state.vscdb-*`
  (holds the pre-3.50 `taskHistory` global state and the encrypted VS Code SecretStorage rows,
  including Roo's provider API keys under `roo_cline_config_*`), `settings.json`, keychain
  items, and every sibling extension directory. The adapter never opens any editor database.
- `roo-code-settings.json` export files (user Downloads) — contain provider profiles including
  API keys (E1: `importExport.ts`). Not under the root; named here so no future variant adds it.
- Workspace-side files (`.roomodes`, `.roo/`, `.rooignore`) — configuration, not conversation;
  outside the storage root; never read.

Rationale: user-authored conversation text is fully contained in `api_conversation_history.json`.
Everything else adds credential, workspace-content, or path exposure with no extraction value.

## 3. Record schema by generation

There is no schema version field anywhere in the store. The adapter fingerprints each task file
by shape (section 6) and never branches on the extension version.

### 3.1 `api_conversation_history.json` (all generations; E1, E2, strong)

A single JSON array (not JSONL), fully rewritten on each save via atomic temp-file replace
(`safeWriteJson`). Each element is an Anthropic `MessageParam` — `role: "user" | "assistant"`,
`content` either a string or an array of blocks (`text`, `image`, `tool_use`, `tool_result`) —
plus Roo extensions that grew over time:

- `ts` (epoch milliseconds): absent in Roo Cline 2.x, present by 3.25 (E2). Nullable for the
  adapter.
- `isSummary: true` marks a context-condensation summary message. Critical: summaries are
  stored with `role: "user"` ("fresh start model: summary is a user message", E1
  `src/core/condense/index.ts`). A user-role record is therefore NOT sufficient evidence of
  human text even before wrapper filtering.
- `condenseId` / `condenseParent`, `truncationId` / `truncationParent` /
  `isTruncationMarker`: non-destructive condense/truncation bookkeeping (late 3.x). Messages
  carrying `condenseParent`/`truncationParent` are original records hidden from the API context;
  they remain the authoritative original text and stay eligible.
- Reasoning persistence fields on assistant-side records (`type: "reasoning"`,
  `encrypted_content`, `reasoning_details`, `reasoning_content`, `id`, `summary`): all excluded
  with their records.

An aborted AI-SDK storage refactor ("ModelMessage" storage, 2026-02-11) was reverted two days
later and never shipped in a stable release; files in that shape are unsupported if ever seen
(E4).

### 3.2 Wrapper generations inside user-role content

Three conventions, with observed version boundaries (E1, E2, E3, strong):

| Generation | Versions | User-text carriers |
| --- | --- | --- |
| G1 "Cline-style XML" | Roo Cline 2.x (2024-11) → 3.41.x (2026-01) | Initial task: `<task>\n…\n</task>`. Tool approval/denial and completion feedback: `<feedback>\n…\n</feedback>` embedded in scaffold prose inside plain user text blocks or string tool results. Followup answers: `<answer>\n…\n</answer>`. Resume instructions: `<user_message>\n…\n</user_message>` inside a `[TASK RESUMPTION]` text block. |
| G2 "native tools" | experimental from ~3.30 (2025-11-13), forced for new tasks in 3.37.0 (2025-12-22) | Tool results become real `tool_result` blocks whose content is a JSON object string such as `{"status":"approved","feedback":"…"}` / `{"status":"denied","feedback":"…"}` / `{"status":"guidance","feedback":"…"}`; the user's words move into the JSON `feedback` value. `<task>`/`<answer>` wrappers still present for task and followup text. |
| G3 "`<user_message>` unification" | 3.42.0 (2026-01-22) → 3.53.0 (final) | All direct user content — initial task, mid-task additions, followup answers, completion feedback — wrapped in `<user_message>\n…\n</user_message>` (text blocks or inside `tool_result` content). JSON status objects as in G2. XML tool protocol removed 3.42 (E4). |

Constant across generations: `<environment_details>\n…\n</environment_details>` blocks are
injected context appended to user messages; @-mention expansion appends file contents (G1:
inline after the wrapper in the same text block, wrapped in `<file_content path="…">`-style
tags; G3: as separate injected text blocks) and rewrites the mention token inside the user's own
text to a quoted path plus a marker phrase such as `(see below for file content)` (E1, E2).
Mixed generations in one store, and G1 wrappers inside a G3-era store (old tasks resumed), are
normal; detection is per record, never per store.

### 3.3 `ui_messages.json` (all generations; E1, strong)

Array of `ClineMessage` objects: `ts` (epoch ms), `type: "say" | "ask"`, `say`/`ask` enum,
`text`, `images` (base64 data URIs), `partial`, plus bookkeeping. Legacy name
`claude_messages.json` predates the fork. User-authored text appears under `say:
"user_feedback"` (raw, unwrapped) — but the initial task prompt is recorded as `say: "text"`,
the same value used for assistant output, so the UI stream cannot structurally separate the
first human message from generated text. This is one concrete reason the V1 requirement
prefers API history over UI history; the adapter uses `ui_messages.json` only for structure-only
timestamp reads and the verification cross-check in section 6.4, never as an extraction source.

### 3.4 Task-level metadata

- `history_item.json` (per task) and `tasks/_index.json`: introduced 2026-02-19, ≈ 3.50.0
  (E4, strong). `HistoryItem`: `id`, `number`, `ts`, `task` (first-message snippet), `workspace`,
  `mode`, token counters, and the delegation fields `rootTaskId`, `parentTaskId`, `childIds`,
  `status`, `delegatedToId` … The adapter reads only: `ts`, `parentTaskId`, `rootTaskId`,
  `status` (eligibility and timestamps). The `task` and `workspace` values are private content;
  they are parsed past but never returned, logged, or hashed into output.
- Older stores keep `taskHistory` inside the editor's `state.vscdb` global state (E5, issue
  #3784); that database is never opened, so pre-3.50 tasks have no adapter-visible delegation
  metadata (see 4.2 and unresolved question 3).

### 3.5 Custom modes and storage

Custom modes (global `settings/custom_modes.yaml`, YAML default since 2025-05-20 ≈ 3.18,
previously `custom_modes.json`; per-project `.roomodes`) change prompts and the `mode` slug
recorded in `HistoryItem`/UI messages. They do not change task file names, locations, or the
wrapper conventions above (E1, E4, strong). No mode-specific storage variant exists; the adapter
ignores modes entirely.

## 4. Inclusion and exclusion rules

### 4.1 Task eligibility (whole directory)

Exclude the entire task directory (no candidate text) when:

1. `history_item.json` exists and carries a non-empty `parentTaskId` or `rootTaskId` differing
   from its own `id`: the task is a subtask spawned by `new_task` delegation; its initial
   `<task>`/`<user_message>` text was composed by the model or orchestrator, not the human.
2. `api_conversation_history.json` is missing, unparseable, or not a JSON array (section 7).

For pre-3.50 stores without `history_item.json`, subtasks are not marked inside their own
directory. Mitigation (deterministic, within one instance): collect the `message` argument of
every `new_task` `tool_use` block across all task files; an initial user message whose wrapper
text exactly matches a collected `new_task` message is excluded as `subtask_initial_message`.
Residual risk is recorded in unresolved question 3.

### 4.2 Candidate user text (per record, API history only)

A record contributes candidate text only if all hold:

1. `role == "user"`.
2. `isSummary` is absent or `false`; `isTruncationMarker` absent or `false`.
3. `type` is absent (records with `type: "reasoning"` are excluded).
4. Text is taken exclusively from recognized user-text carriers:
   - In `text` blocks (or a string `content`): the spans inside `<task>…</task>`,
     `<feedback>…</feedback>`, `<answer>…</answer>`, and `<user_message>…</user_message>`
     wrappers. Text outside these wrappers in the same block (scaffold prose,
     `[TASK RESUMPTION]` boilerplate, `<environment_details>` spans, mention-injected file
     contents) is never extracted.
   - In `tool_result` blocks (string content, or `text` parts of array content): first the same
     wrapper scan; additionally, if the trimmed content parses as a JSON object whose keys
     include `status`, the string value of its `feedback` key (when present and non-empty) is a
     candidate (G2/G3 approval/denial/guidance feedback). All other JSON keys are ignored.
5. Extracted span is non-empty after trimming.

Exclusions (never contribute text): assistant records entirely; `image` and `tool_use` blocks;
`tool_result` content without a wrapper span or JSON `feedback`; `<environment_details>`
content; condensation summaries; any unknown block type.

Wrapper integrity: an opening wrapper tag without its closing tag excludes the whole block and
increments `unbalanced_wrapper`. Nested unknown tags inside a wrapper span are kept as text but
flagged `content_flags: ["unknown_wrapper"]` for normalization quarantine.

### 4.3 Provenance and text status

- Wrapper spans and JSON `feedback` values contain what the user submitted, unmodified — except
  @-mention rewriting: upstream persists user text after mention expansion, which replaces
  `@path`/`@url` tokens inside the user's sentence with a quoted path plus marker text. Rule:
  if an extracted span contains a known mention-expansion marker (the literal phrase
  `(see below for file content)`, `<file_content`, or `'…' (see below`), the utterance gets
  `text_status: "cleaned"` and is quarantined per project spec 4.4; otherwise
  `text_status: "verbatim"`.
- "Enhance prompt" feature: Roo can rewrite the draft prompt with a model before sending; the
  enhanced text is then persisted exactly like typed text with no marker. Structurally
  undetectable; the downstream authorship/normalization layer is the only defense
  (unresolved question 4).
- Modality: `written` for all records. Dictation into VS Code is invisible here; cross-source
  dedup against a voice source is the shared normalizer's job (project spec 4.8, 5.5).
- Authorship basis per kept utterance: `explicit_user_role+wrapper` (or `+json_feedback`).
  Authorship confidence 0.9; reduced to 0.6 with flag `possible_delegated_task` for initial
  messages in pre-3.50 stores (4.1 residual risk).

## 5. Sessions, timestamps, and deduplication

- Session identity: the task directory name (`taskId`). Reported only as a local SHA-256 hash,
  unsalted and never sent anywhere.
  One task = one session; delegation trees are separate sessions (children excluded by 4.1).
- Timestamps: per-record `ts` (epoch ms) when present; fallback order: `history_item.json`
  `ts` (task start), min/max `ts` in `ui_messages.json` (structure-only read of the `ts`
  field), file mtime. Records with no resolvable timestamp are `undated` and excluded from
  bounded period filters, never treated as zero.
- Duplication inside one task file: condensation keeps original records and adds an excluded
  summary — no double counting. Resumes append to the same array. `condenseParent`-tagged
  originals appear once.
- Duplication across files in one instance: a subtask's initial message duplicates the parent's
  `new_task` argument (both excluded by 4.1). The `history_item.json` `task` field and the
  editor-level `taskHistory` snippet duplicate the first message; neither is extracted.
- Duplication across instances: the same editor open natively and via Remote-WSL, or stable
  plus nightly, produces distinct stores of distinct tasks in the normal case; exact-hash plus
  timestamp dedup in the shared normalizer collapses genuine copies. A future ZooCode adapter
  may see migrated or continued copies of these same task directories; cross-adapter text-hash
  dedup covers this (section 8, unresolved question 6).

## 6. Discovery, snapshot, extraction, verification

### 6.1 discover()

1. Build the candidate root list: for each platform-appropriate editor data root (section 1
   list) × {`rooveterinaryinc.roo-cline`, `rooveterinaryinc.roo-code-nightly`}, keep roots that
   exist. Each is one instance. In WSL, only `.vscode-server*` and Linux-native editor roots in
   the WSL home; emit the mounted-Windows hint diagnostic when applicable.
2. Instance with a root but no `tasks/` or an empty `tasks/`: state `found`, zero records.
3. Enumerate `tasks/*/` (skip `_index.json` and lock files). For each task directory apply
   section 4: stream-parse `api_conversation_history.json`, read `history_item.json` if
   present, accumulate candidate message/word/byte counts and min/max timestamps. Large-file
   guard: files above a configured size ceiling (default 256 MB, upstream bloat is a known
   failure mode, E5) are counted `oversized_file` and skipped, never loaded whole.
4. Return only `InstanceInventorySummary` (opaque label such as `Roo Code 1`): never paths,
   editor names beyond the coarse OS field, task IDs, workspace names, or text.
5. Schema fingerprint per instance: set of observed wrapper generations (`g1-xml`,
   `g2-native-json`, `g3-user-message`, `mixed`), presence of `history_item.json`/`_index.json`,
   presence of `ts` on API records.

### 6.2 snapshot()

Plain files; no SQLite, no WAL — the section 4.6 database rules do not apply (E1). Snapshot =
byte copy of each selected task's `api_conversation_history.json`, `history_item.json`, and
`ui_messages.json` (timestamp/cross-check use only) into
`<repository>/runtime/runs/<run-id>/snapshots/roo_code/<instance-hash>/<task-hash>/`, after the
project-spec 3.6 path preflight. Because upstream rewrites whole files atomically via temp-file
rename, a copied file is either the old or the new complete version; after copying, the snapshot
validator re-parses each JSON file and retries the copy once on parse failure before marking it
`malformed_file`. Never copy `checkpoints/`, `task_metadata.json`, or anything in section 2.
The store is opened read-only; no locks are taken; upstream `.lock` files are ignored and never
created or removed by the adapter.

### 6.3 extract()

Runs only against the snapshot. Emits `NormalizedUtterance` records per project spec 4.4 with
modality `written`, text status per 4.3, session hash of the task ID, utterance ID =
deterministic hash of (adapter ID, task ID, record index, block index, span index, text hash).

### 6.4 verify()

Adapter-specific deterministic checks: every utterance maps to a snapshot record satisfying 4.2;
no extracted text contains `<environment_details>`, a wrapper tag, or a JSON `status` key
artifact; opened-path audit shows only allowlisted file names were opened; UI cross-check — for
a sample of tasks, every `say: "user_feedback"` UI entry has a matching extracted utterance by
normalized-text hash (missing matches raise a non-fatal `ui_api_mismatch` diagnostic, since the
API file is authoritative); counts internally consistent.

## 7. Failure behavior

- `api_conversation_history.json` unparseable, non-array, or empty array: task-level
  `malformed_file` / `empty_task`; zero candidates from that task; other tasks unaffected.
- Array elements that are not objects, records without `role`, or content that is neither
  string nor array: record excluded and counted `unrecognized_record`; if such records exceed
  10% of a task's records, the task falls to `unsupported_schema`.
- User-role records that match no carrier in 4.2 contribute nothing silently (this is the
  normal case for tool-result traffic); a task with user-role text records but zero recognized
  wrapper or JSON-feedback carriers across all of them is counted
  `detected, unsupported schema` (possible unknown future/fork convention — never guessed).
- Files in the reverted AI-SDK "ModelMessage" shape or any other unknown top-level shape:
  `detected, unsupported schema`.
- `history_item.json` unparseable: task treated as if the file were absent (pre-3.50 rules),
  plus `metadata_unreadable` diagnostic.
- Root or `tasks/` unreadable: instance `inaccessible` with diagnostic code; never elevate
  privileges, never retried with different credentials.
- The adapter never writes to, locks, or deletes anything under any editor data root.

## 8. Unresolved questions and required behavior when evidence is insufficient

1. Custom storage path (`customStoragePath`): stored in the editor's `settings.json`, which V1
   never opens. Behavior: relocated stores are not auto-discovered; the user may supply the
   path explicitly in run configuration, and discovery then applies all section 2 rules to it.
   No guessing of relocation targets.
2. Exact minor-version boundaries for `ts` introduction on API records and the epoch-ms → UUID
   task-ID switch: unknown (bounded between 2.1.14 and 3.25.0). Behavior: both are
   feature-detected per record/directory; no version gating anywhere.
3. Pre-3.50 subtask detection: without `history_item.json` the `new_task`-argument match in 4.1
   is the only structural signal, and it fails across instances or if the parent task was
   deleted. Behavior: keep the initial-message confidence reduction and
   `possible_delegated_task` flag; the downstream authorship filter quarantines flagged
   utterances that look generated.
4. "Enhance prompt" rewrites are structurally invisible. Behavior: documented recall/precision
   caveat; no adapter-level detection is attempted; normalization-layer generated-text
   screening is the control.
5. `claude_messages.json` under this extension ID: believed practically nonexistent (vestigial
   fork code). Behavior: never parsed; counted `legacy_unmigrated_file` if seen, and research
   is refreshed before any support decision.
6. ZooCode continuation (separate extension ID, first release 3.54.0, 2026-05-16): whether it
   migrates or re-reads `rooveterinaryinc.roo-cline` storage is unverified. Behavior: this
   adapter never opens non-Roo extension directories; if a future `zoocode` adapter is added,
   cross-adapter exact-hash dedup must run before analysis.
7. Roo Code CLI (`apps/cli`, 2026): a late companion CLI exists; its session storage location
   and format are unresearched. Behavior: not discovered; out of scope for `roo_code`.
8. Cursor/Windsurf/Antigravity/VSCodium variant paths are evidenced only by maintainer issues
   (moderate) and are untested. Behavior: discovery enumerates them, but each variant needs the
   release smoke test before its instances count as stable; until then they report stability
   `beta` at the instance level.

## 9. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/roo_code/<variant>/` with a `fixture.json` metadata file. All
content is synthetic; secret-looking values are unmistakably fake (`sk-FAKEFAKEFAKE0000`).

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-g1-xml` | 2.x/3.early-style task: `<task>` initial message (no `ts` on records), `<feedback>` approval/denial prose, `<answer>` followup, `[TASK RESUMPTION]` with `<user_message>`, `<environment_details>` spans, inline mention expansion after `</task>`, tool results as plain user text | exact kept spans; scaffold prose, env details, and mention-injected content never extracted; mention-marker span quarantined as `cleaned` |
| `success-g2-native` | 3.37-style: `<task>` initial, `tool_result` blocks with `{"status":"denied","feedback":"…"}` and `{"status":"approved","feedback":"…"}`, reasoning records with `encrypted_content` | JSON `feedback` values extracted; status JSON without `feedback` ignored; reasoning excluded |
| `success-g3-unified` | 3.42+-style: `<user_message>` everywhere, `ts` on all records, `isSummary` condense record with `condenseId`, originals with `condenseParent`, `history_item.json`, `_index.json` | summary excluded, originals kept once, metadata read for timestamps only |
| `subtask-excluded` | parent task with `new_task` `tool_use` + child task with matching initial message; one child with `history_item.json` `parentTaskId`, one without | both children excluded (metadata path and argument-match path) |
| `empty` | task dir with `[]` history; task dir missing the history file; root with empty `tasks/`; root without `tasks/` | found-empty vs per-task diagnostics |
| `malformed` | truncated JSON file; non-array top level; >10% non-object elements; unreadable `history_item.json` | `malformed_file`, `unsupported_schema`, `metadata_unreadable`; no crash, other tasks unaffected |
| `unsupported` | user-role records with text but no recognized carrier in any record; AI-SDK-shaped file | `detected, unsupported schema`, zero extracted text |
| `migration-mixed` | one store containing a G1 task, a G2 task, and a G3 task; one task mixing G1 and G3 records (resumed old task) | `mixed` fingerprint; per-record detection; all generations extracted correctly |
| `denylist` | root with fake `settings/mcp_settings.json` (fake token), `custom_modes.yaml`, `task_metadata.json`, `checkpoints/` tree, `claude_messages.json`, stray `state.vscdb` | opened-path audit proves none were read |
| `variant-roots` | synthetic editor roots for stable/nightly, Insiders, VSCodium, code-server, `.vscode-server` layouts | each discovered as a separate instance; unknown editor dirs ignored |
| `oversized` | history file above the size ceiling | `oversized_file`, skipped without loading |

Platform matrix: the suite runs on macOS, Linux, WSL, and native Windows runners; path
resolution covers `%APPDATA%` (Roaming) on Windows, `~/Library/Application Support` on macOS,
XDG config on Linux, and `.vscode-server` in WSL. A private smoke test against at least one real
store per claimed platform is required before the stable release gate (adapter guide section 8);
until then every platform row in section 1 remains "untested locally".

## 10. Reproducible read-only inspection commands

Structure-only commands; safe to rerun on a real store. Placeholder paths only. They print field
names, types, and counts — never values.

```bash
# Set ROOT to the instance directory, e.g. (macOS, VS Code stable):
ROOT="$HOME/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline"

# Layout survey (names only)
ls "$ROOT"; ls "$ROOT/tasks" | head -3; ls "$ROOT/tasks" | wc -l

# Per-task file inventory (names only)
find "$ROOT/tasks" -mindepth 2 -maxdepth 2 -type f -exec basename {} \; | sort | uniq -c

# Role / summary-flag / content-shape census of one task (no text printed)
python3 - "$ROOT/tasks/<taskId>/api_conversation_history.json" <<'EOF'
import json, sys, collections
c = collections.Counter()
for m in json.load(open(sys.argv[1], errors="replace")):
    shape = type(m.get("content")).__name__
    c[(m.get("role"), bool(m.get("isSummary")), "ts" in m, shape)] += 1
print(c.most_common())
EOF

# Wrapper-generation markers (booleans only, no content)
python3 - "$ROOT/tasks/<taskId>/api_conversation_history.json" <<'EOF'
import json, sys, collections
c = collections.Counter()
def texts(content):
    if isinstance(content, str): yield content
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text": yield b.get("text") or ""
for m in json.load(open(sys.argv[1], errors="replace")):
    if m.get("role") != "user": continue
    for t in texts(m.get("content")):
        c[tuple(tag in t for tag in
            ("<task>", "<feedback>", "<answer>", "<user_message>", "<environment_details>"))] += 1
print(c.most_common())
EOF
```

Never run `cat`, `jq .`, or any command that prints message values against a real store.
