# Cursor source specification

Adapter ID: `cursor`
Status: reviewed source specification (spec section 4.2 research gate)
Stability: **beta** (spec sections 1.4 and 4.7; never auto-selected, user must opt in)
Research log: `temp/findings/cursor-source-research.md` (evidence IDs E1–E10 cited below)
Tested application version: Cursor 3.14.7, macOS (direct structural observation of the real
local store, authorized by the project owner; labeled reverse engineering)
Access date for all cited evidence: 2026-08-08

Cursor is a closed-source VS Code fork. It publishes no schema for its chat storage (E3), and
community maintainers have repeatedly been broken by silent format changes. The adapter
therefore supports exactly one structurally verified storage generation, feature-detects it per
record, and reports every other generation as "detected, unsupported schema" that is
inventoried but contributes no analyzable text (spec 4.7).

## 1. Platform status and storage locations

Never infer one platform's behavior from another. Each row carries its own evidence.

| Platform | Status | User-data root | Evidence |
| --- | --- | --- | --- |
| macOS | Supported (beta), tested | `<home>/Library/Application Support/Cursor/User/` | E1 (strong) |
| Windows (native) | Claimed by community tools, untested; extraction gated on a Windows fixture + smoke test | `%APPDATA%\Cursor\User\` (roaming) | E3, E4 (moderate) |
| Linux (native) | Claimed by community tools, untested; extraction gated on a Linux smoke test | `<home>/.config/Cursor/User/` | E2, E3, E4 (moderate) |
| WSL | Not supported in V1; fails closed with a hint | Chat state is written client-side on the Windows host (`%APPDATA%\Cursor\...`), reachable from WSL only via `/mnt/<drive>/...` | E3 (moderate) |

Details:

- The chat store of record is the **global** SQLite database
  `<user-data-root>/globalStorage/state.vscdb` (tables `ItemTable`, `cursorDiskKV`). Deleting
  it destroys chat history across all projects, which confirms it is the authoritative store,
  not a cache (E1, E8).
- Per-workspace databases `<user-data-root>/workspaceStorage/<hash>/state.vscdb` hold workspace
  UI state plus the composer-to-workspace index; `<hash>/workspace.json` maps the hash to the
  private project folder (key `folder`) (E1, strong; E4 moderate).
- A second home-directory tree `<home>/.cursor/` exists on macOS (config, extensions,
  `projects/`, `ai-tracking/`, `plans/`, optionally `chats/` for the Cursor CLI). It is
  denylisted except as noted in section 3 (E1, E6).
- WSL: Cursor's GUI runs on the Windows host. Editing a WSL project uses a remote server under
  the WSL-side `<home>/.cursor-server/`, but chat/composer state is persisted client-side into
  the Windows-host global database (E3, moderate). The host database is a live WAL-mode SQLite
  file; V1 does not attempt cross-DrvFS snapshots of a live database (spec 1.3). In WSL the
  adapter reports `not_found` for the WSL side, and when
  `/mnt/<drive>/Users/<user>/AppData/Roaming/Cursor/User/globalStorage/state.vscdb` exists it
  emits a hint diagnostic directing the user to run the audit from native Windows.
  `<home>/.cursor-server/data/User/workspaceStorage` (remote/SSH variant, E4) is untested:
  detected, unsupported schema.
- Privacy Mode is a server-side training/retention control; no evidence was found that it
  changes local persistence (E9, moderate). Discovery treats an empty store as "found, empty".
- Retention: no automatic local expiry of chat data was observed or documented; the global
  database grows without bound (multi-GB stores are normal, E1, E6). Users may manually delete
  history; short or missing history is normal.

## 2. Storage generations

Cursor has changed chat persistence at least four times. Only G4 is supported for extraction.

| Gen | Approx. era | Where user text lives | Adapter policy |
| --- | --- | --- | --- |
| G1 "aichat tabs" | ~2023 – late 2024 | Workspace `ItemTable` key `workbench.panel.aichat.view.aichat.chatdata`: JSON `tabs[]` each with `bubbles[]`, bubble `type` `"user"`/`"ai"` | Detected, unsupported schema; inventoried, no text (E5, weak-moderate) |
| G2 "workspace composer" | ~late 2024 – 2025 | Workspace `ItemTable` key `composer.composerData` with full conversation data inline; migration flags `hasMigratedComposerData`, `hasMigratedMultipleComposers` remain in current stores | Detected, unsupported schema when conversation content is present inline (E1 for the flags, strong; E4 for the migration, moderate) |
| G3 "global composer, embedded conversation" | ~2025 | Global `cursorDiskKV` `composerData:<uuid>` with a populated `conversationMap`/`conversation` (community: composerData `_v` 1–2) | Detected, unsupported schema; inventoried, no text (E2, moderate) |
| G4 "global bubble store" (current, **verified**) | ~2025-? → current (3.14.x) | Global `cursorDiskKV`: `composerData:<composerUuid>` headers (`_v` 10–16 observed) + one row per message `bubbleId:<composerUuid>:<bubbleUuid>` (bubble `_v` 3 observed) | Supported for extraction (E1, strong; E2 corroborates) |
| G5 "Cursor CLI store" | 2025 → current | `<home>/.cursor/chats/<hash>/<uuid>/store.db` (SQLite, `meta` + `blobs` tables) | Detected, unsupported schema; inventoried presence only, no text (E6, moderate; not present on the tested machine) |

Exact version boundaries for G1→G2→G3→G4 are unpublished and were not established. The adapter
therefore never branches on the application version; it feature-detects per key and per record
(`_v` fields, key presence, `conversationMap` emptiness).

## 3. Files and keys that must never be opened

The adapter opens only:

1. `globalStorage/state.vscdb` snapshot — reads restricted to `cursorDiskKV` rows with key
   prefixes `composerData:` and `bubbleId:`, and the `ItemTable` row
   `composer.composerHeaders`.
2. `workspaceStorage/<hash>/state.vscdb` snapshots — reads restricted to the `ItemTable` row
   `composer.composerData` (composer-to-workspace index; headers only).
3. `workspaceStorage/<hash>/workspace.json` — only the `folder` value, used solely to build the
   hashed instance key and opaque label; the path itself is never returned, logged, or stored.

Everything else is denylisted. Explicitly forbidden (E1, strong):

- `ItemTable` keys `cursorAuth/accessToken`, `cursorAuth/refreshToken`, `cursorAuth/cachedEmail`
  and all other `cursorAuth/*` keys (OAuth tokens and account identity).
- All `ItemTable` keys beginning `secret://` (encrypted extension secrets, MCP client secrets).
- `telemetry.*`, `storage.serviceMachineId`, `machineid`, `storage.json` (device/telemetry
  identifiers), `openai.chatgpt`, `vscode.microsoft-authentication`.
- `cursorDiskKV` prefixes: `agentKv:` (content-addressed blob store mixing raw file contents,
  model-request messages, and binary blobs), `messageRequestContext:` (full injected request
  context), `checkpointId:`, `codeBlockDiff:`, `codeBlockPartialInlineDiffFates:`,
  `inlineDiff*`, `patch-graph:`, `ofsContent:`, `expectedContent-*`, `composer.content.*`,
  `composerVirtualRowHeights:`, and any unlisted prefix.
- `state.vscdb.backup` (both global and workspace; stale duplicate of the store).
- `User/History/` (local file-save history), `Backups/`, `Cookies*`, `Local Storage/`,
  `Session Storage/`, `WebStorage/`, `Network Persistent State`, `Trust Tokens*`,
  `Preferences`, `sentry/`, `Crashpad/`, `logs/`, caches, and the extension storage
  directories `globalStorage/anysphere.cursor-retrieval` (code embeddings),
  `globalStorage/anysphere.cursor-commits` (checkpoint diffs and file snapshots),
  `globalStorage/anysphere.cursor-mcp`, and every other extension directory.
- The entire `<home>/.cursor/` tree, including `mcp.json` (may embed credentials),
  `ai-tracking/ai-code-tracking.db`, `prompt_history.json` (duplicate prompt text without
  structure, E6), `plans/`, `snapshots/`, `browser-logs/`,
  `projects/<encoded-path>/agent-transcripts/**` (model-request transcripts whose user lines
  wrap the prompt in `<user_query>` and attach `<attached_files>`-style context; verified
  duplicates of the bubble store, E1), and `chats/**` except the presence probe allowed for the
  G5 inventory (directory existence and file names only; `store.db` contents are never read).
- `<home>/.cursor-server/**` (WSL/remote server side).

Rationale: the only clean user-authored channel is the G4 bubble store. Every other location
either duplicates that text with injected context (dedup hazard and provenance loss) or
contains credentials, identifiers, code, or telemetry that add risk and no extraction value.

## 4. Record schema (G4, verified on macOS at 3.14.7; E1 unless noted)

Both tables are simple key-value stores:

```sql
CREATE TABLE ItemTable    (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
```

The global database runs in WAL journal mode; `-wal`/`-shm` sidecars may exist while Cursor is
running (snapshot rules, section 8.2).

### 4.1 `composerData:<composerUuid>` (one JSON object per conversation)

Observed `_v` values 10–16 coexisting in one store (minor revisions; key set grows, shape is
stable). Fields the adapter reads:

- `composerId` (UUID string; must match the key suffix, else `composer_id_mismatch`).
- `_v` (int).
- `createdAt` (epoch milliseconds, int), `lastUpdatedAt` (epoch ms, optional).
- `unifiedMode` (string: `"chat"`, `"agent"`, `"plan"` observed; E2 also lists `"edit"`).
- `fullConversationHeadersOnly`: ordered array of `{bubbleId, type}` with optional
  `serverBubbleId`, `grouping`, `contentHeightHint`. This is the authoritative message order
  and the cheap user/assistant index (`type` 1 = user, 2 = assistant).
- `conversationMap` (dict): empty `{}` in G4. A non-empty `conversationMap` or a legacy
  `conversation` array marks a G3 record: unsupported, no extraction from it.
- Eligibility flags: `isBestOfNSubcomposer`, `isBestOfNParent`, `isSpec`, `subComposerIds`
  (list of composer UUIDs), `isDraft`, `status` (`"completed"`, `"aborted"`, `"none"`).
- Ignored context/config fields (never returned): `context`, `codeBlockData`,
  `originalFileStates`, `modelConfig`, `usageData`, `name`, `subtitle`, git worktree fields,
  token/line counters, and all other keys. Unknown keys are tolerated.

### 4.2 `bubbleId:<composerUuid>:<bubbleUuid>` (one JSON object per message)

Observed `_v: 3` on every parsed bubble. Envelope fields common to both types: `_v`, `type`
(int; 1 = user, 2 = assistant), `bubbleId`, `requestId`, `createdAt` (ISO 8601 UTC with
millisecond precision and `Z`; present on all observed current-era bubbles, treated as
nullable), `isAgentic`, `unifiedMode` (int; meaning unverified, not used), plus several dozen
context-capture fields (attached chunks, lints, console logs, diffs, todos, etc.) that are
never extracted.

User bubbles (`type: 1`) additionally carry:

- `text` (string): the plain-text content of the composer input box.
- `richText` (string): serialized Lexical editor state (`root` → `children` tree of
  `paragraph`/`text`/`linebreak`/mention nodes). Used only for the fidelity cross-check.
- `isNudge` (bool), `isQuickSearchQuery` (bool), `skipRendering` (bool), `isPlanExecution`
  (bool), optional `toolFormerData` (on user bubbles observed to contain only bookkeeping
  `{additionalData: {status}}`; not an exclusion signal), `context` (attached-context dict,
  never extracted), `modelInfo`.

Assistant bubbles (`type: 2`) carry `text` (mostly empty), `codeBlocks`, `thinking`,
`toolFormerData` (tool name, args, result), `serverBubbleId`, summaries, and are excluded
entirely.

### 4.3 `composer.composerHeaders` (global `ItemTable`) and workspace index

- Global `ItemTable` key `composer.composerHeaders`: `{allComposers: [{type, composerId,
  createdAt, unifiedMode, isArchived, isDraft, isBestOfNSubcomposer, isSpec, ...}]}` — a fast
  header list for discovery.
- Workspace `ItemTable` key `composer.composerData`: `{allComposers: [{composerId, name,
  createdAt, lastUpdatedAt, unifiedMode, ...}], selectedComposerIds, hasMigratedComposerData,
  hasMigratedMultipleComposers}` — associates composer UUIDs with the workspace. Only
  `composerId` values are read from it.

## 5. Provenance of text fields (rawness)

- **Storage fidelity (verified):** the user-bubble `text` field is the UI-level composer-buffer
  content, not the model-request text. Injected context (attached files, rules, project
  layout, `<user_query>` wrappers) lives in separate stores (`messageRequestContext:`,
  `agentKv:`, `agent-transcripts`) and does not contaminate `text`. A value-free structural
  cross-check on the real store compared `text` against the text nodes of the `richText`
  Lexical tree for ~2,000 user bubbles: 67% matched exactly after whitespace normalization,
  10% were substring matches, and the remainder differed only around mention-chip and
  code-fence serialization; none showed injected wrapper tags (E1). Cursor applies no observed
  rewriting or enhancement between the input box and the stored `text`.
- **Input-method provenance (unproven):** Cursor 2.0 (2025-10) added a native Voice Mode that
  batch-transcribes speech directly into the agent prompt field, upgraded again in 3.1
  (E7, vendor changelog: strong for existence). No per-bubble field distinguishing typed,
  dictated, or pasted input was found anywhere in the bubble, composer, or `ItemTable` key
  space of the tested store (E1; caveat: the tested store contains no known voice-mode usage,
  so a marker that only appears on dictated bubbles cannot be ruled out — unresolved question
  1). Whether the built-in STT output is raw or cleaned is unknown.
- Consequences for extraction from the verified G4 variant:
  - `text_status`: `verbatim` with respect to the composer buffer (structurally verified).
  - `modality`: `written` under the spec 5.5 operational convention (coding-agent text not
    positively matched to a raw voice source is `written`). The native Voice Mode means some
    `written` Cursor text may actually be machine-transcribed; this undisclosed contamination
    risk is a stated reason the adapter ships as beta and is never auto-selected.
- All other generations (G1, G2, G3, G5) and platforms without a completed structural
  verification: rawness unknown → inventory only, no analyzable text (spec 4.7).

## 6. Inclusion and exclusion rules (G4)

### 6.1 Composer (session) eligibility

A composer contributes candidate text only if all hold:

1. `composerData:<uuid>` parses as a JSON object with int `_v` and matching `composerId`.
2. `conversationMap` is absent or empty, and no legacy `conversation` array is present
   (otherwise: G3, unsupported for that composer).
3. `isBestOfNSubcomposer` is absent or `false` (best-of-N sub-runs duplicate the user prompt
   across N automatic sub-composers).
4. The composer's UUID does not appear in any other composer's `subComposerIds` (agent-spawned
   sub-composers; their "user" bubbles may be orchestrator-composed). Fail closed.

`isDraft`, `isSpec`, `status`, and `unifiedMode` do not exclude a composer; drafts and aborted
runs may still contain genuine typed prompts.

### 6.2 Bubble inclusion

A bubble contributes candidate user-authored text only if all hold:

1. The row key matches `bubbleId:<eligible composerUuid>:<bubbleUuid>` and the value parses as
   a JSON object.
2. `_v == 3`. Any other `_v`: exclude and count `unsupported_bubble_version`; if such bubbles
   dominate an instance, the instance falls to `unsupported_schema`.
3. `type == 1`.
4. `isNudge` is absent or `false` (semantics unverified; observed on a minority of user
   bubbles; fail closed, counted as `excluded_nudge`).
5. `isQuickSearchQuery` is absent or `false`.
6. `skipRendering` is absent or `false`.
7. `text` is a non-empty string after trimming.

Text is taken only from `text`. `richText` is parsed solely for the section 8.4 fidelity
check; images, attachments, context fields, and tool data are never extracted. Per-utterance
metadata: bubble UUID (utterance ID basis), composer UUID (session basis), `createdAt`
timestamp (nullable), modality `written`, text status `verbatim`, authorship basis
`explicit_user_role_type1`.

Paste artifacts: pasted text is not structurally marked; the shared normalization layer
removes copied/pasted/code-like material (spec 4.5). The adapter sets
`content_flags: ["possible_paste"]` above a configured length threshold, mirroring the
`claude_code` adapter.

Credential safety: the adapter never opens denylisted keys or files (section 3). Secrets typed
into prompts are handled downstream by the privacy pipeline.

## 7. Sessions, timestamps, workspace association, deduplication

- Session identity: the composer UUID. Session hash per the shared hashing rules; message
  order comes from `fullConversationHeadersOnly`, not from key order.
- Timestamps: bubble `createdAt` (ISO 8601 UTC). Fallback when absent: composer `createdAt`
  (epoch ms). Utterances with neither are `undated` (kept for counting, excluded from bounded
  period filters).
- Instance model: one Cursor user profile (one global database) is one source instance.
  Workspace sub-grouping for opaque labels uses the workspace index (section 4.3): composers
  listed in a workspace's `composer.composerData` map to that workspace's opaque label;
  unmapped composers form a residual group. Private folder paths from `workspace.json` are
  hashed and never returned (spec 2.4).
- In-adapter duplicates the design must expect:
  - Best-of-N: parent and sub-composers repeat one human prompt; rule 6.1.3 removes the
    sub-copies, and exact text-hash + near-identical-timestamp dedup collapses any residue.
  - Workspace transfer / duplicated composers (`chat.workspaceTransfer` exists in the store):
    composer UUIDs are globally unique; identical `(composerId, bubbleId)` pairs seen through
    two instance paths collapse to one canonical utterance (earliest snapshot wins).
  - `state.vscdb.backup` is never read, so stale-copy duplicates cannot enter.
  - `aiService.prompts` / `aiService.generations` (workspace `ItemTable`), `agentKv:`,
    `messageRequestContext:`, `agent-transcripts`, and `prompt_history.json` all duplicate
    prompt text with worse provenance; they are denylisted, so no cross-store dedup inside
    this adapter is needed.
- Cross-source duplicates (for example dictation via Wispr Flow pasted into Cursor) are the
  shared normalizer's job (spec 4.8); this adapter supplies text hashes and timestamps.

## 8. Discovery, snapshot, extraction, verification

### 8.1 discover()

1. Resolve the platform user-data root (section 1). If `globalStorage/state.vscdb` is missing:
   `not_found`. If present but zero composers: `found`, empty.
2. Open the live global database read-only (`mode=ro` URI, busy timeout, no locks taken) for
   inventory only. Enumerate `composerData:` rows with an index-friendly key-range predicate
   (`key >= 'composerData:' AND key < 'composerData;'`), classify each composer's generation
   (G3 vs G4) and eligibility (6.1).
3. For eligible composers, read `fullConversationHeadersOnly` and fetch only the `type == 1`
   bubble rows by exact key (point lookups). This avoids scanning assistant/tool rows, which
   dominate the store; multi-GB databases must still meet the five-minute onboarding target.
4. Accumulate candidate message, word, and byte counts and min/max timestamps per instance and
   per workspace group. Apply section 6 rules identically to the later extraction. Never
   print, log, or return text.
5. Enumerate `workspaceStorage/*/state.vscdb` for the composer-to-workspace index; read
   `workspace.json` `folder` only into the path hash.
6. Report legacy material without reading text: presence of G1/G2 keys per workspace, G3
   composers, and a `<home>/.cursor/chats/` directory (G5) are each reported as
   "detected, unsupported schema" inventory lines with stable diagnostic codes.
7. Storage fingerprint: set of observed composer `_v` values, bubble `_v` values, and
   generation markers (for example `g4;composer_v=10-16;bubble_v=3`).

### 8.2 snapshot()

- The global store is a live WAL-mode SQLite database that Cursor holds open. Snapshot uses
  the SQLite backup API (or, when Cursor is not running, a byte copy of `state.vscdb` plus its
  `-wal` and `-shm` sidecars) into `<repository>/temp/runtime/<run-id>/snapshots/cursor/`,
  after the spec 3.6 path-safety preflight. Never treat the live file as an immutable flat
  file (spec 4.6).
- Free-space preflight: the global database can exceed several GB. If free space is below the
  database size plus margin, snapshotting fails closed with `SNAPSHOT_INSUFFICIENT_SPACE`;
  no partial snapshot is used.
- Workspace databases selected for the index are snapshotted the same way (they are small).
- If the backup API reports the database is locked beyond the bounded retry budget:
  `inaccessible` with a diagnostic advising the user to close Cursor; never force, never
  elevate.
- Snapshot manifest records file sizes, mtimes, and SHA-256 hashes; snapshot copies are
  written `0600`/`0700`. The adapter never writes to, locks, truncates, or deletes anything
  under either Cursor root.

### 8.3 extract()

Runs only against the snapshot. Emits `NormalizedUtterance` records (spec 4.4) with modality
`written`, text status `verbatim`, and a source-path hash of the canonical original path.

### 8.4 verify()

Adapter-specific deterministic checks: every utterance maps to a snapshot bubble row passing
section 6; the `richText` fidelity cross-check passes on a sampled subset (text equals or is
contained in the concatenated Lexical text nodes after whitespace normalization; persistent
mismatch beyond the mention/code-fence tolerance raises `richtext_mismatch`); no denylisted
key or file appears in the opened-path/key audit; counts are internally consistent; no
utterance text begins with a known injected wrapper tag (`<user_query`, `<attached`,
`<additional_data`, `<custom_instructions`, `<environment` — their presence in bubble text
would indicate a schema change, failing the instance closed with `wrapper_leak_detected`).

## 9. Failure behavior

- Database missing tables `ItemTable`/`cursorDiskKV`, not a database (`SQLITE_NOTADB`),
  corrupt, or encrypted: instance `unsupported_schema` (never bypass encryption).
- Composer row unparseable or `_v` outside every known range: that composer is
  `unsupported_schema`; other composers continue. If all composers are unsupported, the
  instance is "detected, unsupported schema" with no candidate counts.
- Header references a missing bubble row: count `missing_bubble` and continue; if more than
  10% of an eligible composer's user-bubble references are missing, mark that composer
  `unsupported_schema` (possible mid-migration store).
- Malformed JSON in more than 10% of sampled bubble rows: instance `unsupported_schema`.
- Locked database, permission errors, unreadable root: `inaccessible` with diagnostic code;
  bounded retries only.
- Unknown future keys, key prefixes, or JSON fields: ignored and counted, never guessed at.
- G1/G2/G3/G5 material and untested platform variants: always "detected, unsupported schema";
  inventory only, zero extracted text (spec 4.2, 4.7).

## 10. Unresolved questions and required behavior when evidence is insufficient

1. **Voice Mode marker:** whether a bubble created through native Voice Mode carries any
   distinguishing field (the tested store contains no known dictated bubble). Required
   behavior now: modality `written` by the spec 5.5 convention, beta stability, adapter never
   auto-selected. Required follow-up before any stable promotion: generate dictated bubbles on
   a test install and re-inspect; if a marker exists, dictated bubbles must be excluded until
   the STT raw-vs-cleaned question is settled; if none exists, the limitation stays in the
   compatibility matrix.
2. **`isNudge` and bubble-level `unifiedMode` semantics:** unverified. Behavior: exclude
   `isNudge` bubbles (fail closed); ignore bubble `unifiedMode`.
3. **G1/G2/G3 exact shapes and version boundaries:** community-documented only; no local
   specimen. Behavior: unsupported schema, inventory only. Adding support requires refreshed
   research plus fixtures from a real specimen (spec 4.2).
4. **Windows and Linux G4 equivalence:** paths are community-corroborated, schema equivalence
   is plausible (same Electron codebase) but unverified. Behavior: discovery may inventory;
   extraction on those platforms is disabled until a platform fixture and private smoke test
   verify the same G4 structure.
5. **Cursor CLI (`~/.cursor/chats/**/store.db`) schema:** blob-store layout known only from
   secondary description (E6). Behavior: presence-only inventory, unsupported schema.
6. **`composerData` `_v` outside 10–16 or bubble `_v` other than 3:** unknown revisions.
   Behavior: per-record `unsupported_bubble_version` / composer `unsupported_schema`; research
   refresh required before supporting a new `_v` (spec 4.2).
7. **Remote/SSH server-side stores (`.cursor-server`):** untested. Behavior: detected,
   unsupported schema.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/cursor/<variant>/` with a `fixture.json` metadata file. All
content is synthetic; secret-looking values are unmistakably fake (for example
`sk-FAKEFAKEFAKE0000`). SQLite fixtures are generated by a checked-in builder script so no
binary blobs need hand maintenance.

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-g4` | Global DB: composers `_v` 10 and 16, bubbles `_v` 3 (user with `text`+`richText`, assistant with `codeBlocks`/`toolFormerData`), headers in order; workspace DB + `workspace.json` index | exact kept-utterance set, order from headers, workspace grouping, assistant/tool exclusion |
| `flags-excluded` | User bubbles with `isNudge: true`, `isQuickSearchQuery: true`, `skipRendering: true`, empty `text`; composer with `isBestOfNSubcomposer: true`; parent listing a `subComposerIds` child | each exclusion rule fires with its counter |
| `legacy-g3-embedded` | Composer with non-empty `conversationMap` | detected, unsupported schema; zero text; inventory line present |
| `legacy-g2-workspace` | Workspace `composer.composerData` with inline conversation content, no global rows | unsupported schema inventory, no extraction |
| `legacy-g1-aichat` | Workspace `workbench.panel.aichat.view.aichat.chatdata` with `tabs[]`/`bubbles[]` | unsupported schema inventory, no extraction |
| `empty` | Global DB with tables but no composer rows; missing global DB | found-empty vs not-found |
| `malformed` | Bubble rows with truncated JSON, non-object JSON; one composer over the 10% threshold; header referencing a missing bubble | thresholds, `missing_bubble`, no crash |
| `unsupported-version` | Bubbles `_v: 99`, composer `_v: 1` | version fail-closed counters |
| `wal-live` | DB with uncommitted `-wal` content | backup-API snapshot sees a consistent state |
| `denylist` | Store containing fake `cursorAuth/accessToken`, `secret://…`, `agentKv:` and `messageRequestContext:` rows, `state.vscdb.backup`, fake `~/.cursor/` tree with `mcp.json` and `agent-transcripts` | opened-path/key audit proves none were read |
| `richtext-mismatch` | User bubble whose `text` diverges from its `richText` beyond tolerance | `richtext_mismatch` diagnostic |
| `windows-paths` / `linux-paths` | Same `success-g4` content under `%APPDATA%\Cursor\...` and `.config/Cursor/...` layouts | path resolution per platform; extraction stays gated until the platform smoke test is recorded |
| `wsl-hint` | Simulated `/mnt/c/Users/<fake>/AppData/Roaming/Cursor/...` presence | fail-closed WSL behavior plus hint diagnostic |

Platform matrix: the pytest suite runs on macOS, Linux, native Windows, and WSL runners. Beta
release requires the macOS smoke test (done during this research, outputs not committed);
stable promotion additionally requires Windows and Linux smoke tests, the Voice Mode
investigation (10.1), and a research refresh.

## 12. Reproducible read-only inspection commands

Structure-only commands used for this research; safe to rerun. Placeholder paths only. Always
inspect a copy, never the live file; delete the copy afterwards. They print key names, field
names, types, and counts — never values.

```bash
SRC="<home>/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
cp "$SRC" /tmp/cursor-inspect.vscdb   # remove when done

sqlite3 /tmp/cursor-inspect.vscdb ".schema"

# Key-family census
sqlite3 /tmp/cursor-inspect.vscdb "SELECT CASE WHEN instr(key,':')>0 THEN substr(key,1,instr(key,':')) ELSE key END, COUNT(*) FROM cursorDiskKV GROUP BY 1 ORDER BY 2 DESC LIMIT 20;"

# Bubble schema profile (field names and JSON types only)
python3 - /tmp/cursor-inspect.vscdb <<'EOF'
import sqlite3, json, sys, collections
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
kf = collections.defaultdict(collections.Counter); vv = collections.Counter()
for (val,) in db.execute("SELECT value FROM cursorDiskKV WHERE key >= 'bubbleId:' AND key < 'bubbleId;' LIMIT 3000"):
    try: o = json.loads(val)
    except Exception: continue
    if not isinstance(o, dict): continue
    vv[(o.get("_v"), o.get("type"))] += 1
    for k, v in o.items(): kf[o.get("type")][f"{k}:{type(v).__name__}"] += 1
print("(_v, type) counts:", dict(vv))
for t, c in kf.items(): print("type", t, c.most_common(40))
EOF

# Composer schema profile
python3 - /tmp/cursor-inspect.vscdb <<'EOF'
import sqlite3, json, sys, collections
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
kf = collections.Counter(); vv = collections.Counter(); emptymap = collections.Counter()
for (val,) in db.execute("SELECT value FROM cursorDiskKV WHERE key >= 'composerData:' AND key < 'composerData;'"):
    try: o = json.loads(val)
    except Exception: continue
    vv[o.get("_v")] += 1
    emptymap[bool(o.get("conversationMap"))] += 1
    kf.update(f"{k}:{type(v).__name__}" for k, v in o.items())
print("_v:", dict(vv), "conversationMap nonempty:", dict(emptymap)); print(kf.most_common(50))
EOF

rm /tmp/cursor-inspect.vscdb
```

Never run `SELECT value` output to a terminal, `jq .`, `.dump`, or any command that prints
record values from a real store. Do not open `ItemTable` rows other than by exact key name,
and never the `cursorAuth/*` or `secret://*` rows.
