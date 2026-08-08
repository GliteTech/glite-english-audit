# Cursor source specification

Adapter ID: `cursor`
Status: reviewed source specification (spec section 4.2 research gate)
Stability: **beta** (spec sections 1.4 and 4.7; never auto-selected, user must opt in)
Research log: `temp/findings/cursor-source-research.md` (evidence IDs E1–E10 cited below)
Rawness evidence: `temp/findings/cursor-rawness-evidence.md` (evidence ID **E11**, section 5)
Tested application version: Cursor 3.14.7, macOS (direct structural observation of the real
local store, authorized by the project owner; labeled reverse engineering)
Access date for all cited evidence: 2026-08-08

Cursor is a closed-source VS Code fork. It publishes no schema for its chat storage (E3), and
community maintainers have repeatedly been broken by silent format changes. The adapter
therefore supports exactly one structurally verified storage generation, feature-detects it per
record, and reports every other generation as "detected, unsupported schema" that is
inventoried but contributes no analyzable text (spec 4.7).

**State-of-knowledge change (2026-08-08).** Spec 4.7 requires Cursor rawness to be treated as
`unknown` "unless a tested variant proves otherwise". That condition is now satisfied for one
variant, and only for that variant: on the tested G4 / macOS store, the stored user text is a
faithful serialization of the composer editor state, not a cleaned or model-processed form
(E11, section 5). This specification therefore moves from *inventory-only for every Cursor
variant* to *per-bubble reconciliation for the proven variant, inventory-only and fail-closed
for everything else*. Stability remains **beta**: beta means the adapter is never selected by
default and the user must opt in explicitly (spec 1.4).

## 1. Platform status and storage locations

Never infer one platform's behavior from another. Each row carries its own evidence.

| Platform | Status | User-data root | Evidence |
| --- | --- | --- | --- |
| macOS | Supported (beta), tested; the only platform where text can be `verbatim` | `<home>/Library/Application Support/Cursor/User/` | E1 (strong), E11 (strong, rawness) |
| Windows (native) | Claimed by community tools, untested; inventory only, all text `unknown`; extraction gated on a Windows fixture + smoke test **including its own rawness comparison** | `%APPDATA%\Cursor\User\` (roaming) | E3, E4 (moderate) |
| Linux (native) | Claimed by community tools, untested; inventory only, all text `unknown`; extraction gated on a Linux smoke test **including its own rawness comparison** | `<home>/.config/Cursor/User/` | E2, E3, E4 (moderate) |
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
| G4 "global bubble store" (current, **verified**) | ~2025-? → current (3.14.x) | Global `cursorDiskKV`: `composerData:<composerUuid>` headers (`_v` 10–16 observed) + one row per message `bubbleId:<composerUuid>:<bubbleUuid>` (bubble `_v` 3 observed) | Supported for extraction on macOS, per-bubble rawness gate (section 5.4); rawness proven for this variant (E1, strong; E2 corroborates; E11 strong for rawness) |
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
  `paragraph`/`text`/`tab`/`linebreak`/`mention` nodes). This is the literal content of the
  prompt box at send time and the input to the section 5.4 rawness gate: a user bubble is only
  `verbatim` when its stored `text` reconciles with the projection of this field. Present on
  96.5% of user bubbles in the tested corpus; absent or unusable on 3.5%, which stay `unknown`
  (E11).
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

### 5.1 Evidence status

| Field | Value |
| --- | --- |
| Evidence ID | E11 (`temp/findings/cursor-rawness-evidence.md`) |
| Nature | Owner-authorized reverse engineering of one real macOS installation |
| Date | 2026-08-08 |
| Scope | One user, one machine, macOS, G4, composer `_v` 10–16, bubble `_v` 3 |
| Corpus size | 4,626 user bubbles (`type == 1`, non-empty `text`) |
| Strength | **Strong for this variant only** — no other platform, generation, or `_v` range inherits it |

No prompt text, path, project name, or other personal value from that inspection is reproduced
here or in any committed file; only structural and statistical results are recorded.

### 5.2 Method

For every user bubble carrying non-empty `text`, the sibling `richText` field — the serialized
Lexical editor state, i.e. the literal content of the prompt box at send time — was parsed and
projected back to plain text under a fixed projection rule, then compared with the stored
`text`:

1. `root` children are paragraphs, joined with newlines.
2. Within a paragraph, `text` nodes concatenate; `tab` and `linebreak` nodes render literally.
3. `mention` nodes render as their display name.

Observed node-type census across the corpus: `paragraph` 329,350; `text` 319,025; `mention`
9,976; `root` 4,462; `tab` 222; `linebreak` 1.

### 5.3 Result

| Comparison | Count | Share |
| --- | ---: | ---: |
| Stored `text` byte-identical to the editor-state projection | 2,798 | 60.5% |
| Identical after whitespace normalization | 959 | 20.7% |
| Still different | 705 | 15.2% |
| No editor state stored (`richText` absent or unusable) | 164 | 3.5% |

**Verbatim-equivalent: 81.2%** (byte-identical plus whitespace-normalized).

The residual is explained, not unexplained. Every inspected case in the "still different"
bucket differed for the same reason: the stored `text` keeps the `@` sigil on a file mention
while the mention node's display name omits it. Once that one-character sigil is accounted for
on mention nodes, divergence approaches zero. The 15.2% bucket is therefore a projection
artifact of the comparison rule, not evidence of rewriting.

Two independent corroborations that no rewriting pass exists:

- **Typo survival.** Typographical errors survive intact into stored `text` (observed: a
  misspelled tool name and a misspelled verb, both in user prompts). Any cleaning, spell-fixing,
  or model-rewriting stage between the input box and storage would have removed them. This is
  the decisive check for our purpose: the audit measures exactly the kind of surface error a
  cleaning pass destroys.
- **Failure mode of the naive projection.** An earlier projection that dropped paragraph
  boundaries appeared to show 51.8% divergence. Joining paragraphs correctly collapsed that
  divergence — the signature of a faithful serialization, and the opposite of what a rewriting
  pipeline would produce (a rewriting pipeline's divergence does not disappear when the reader's
  projection rule is fixed).

**Conclusion.** For the tested variant, Cursor stores the user's prompt verbatim; `text` is a
faithful serialization of the editor state. This satisfies the spec 4.7 "tested variant proves
otherwise" condition for that variant and for nothing beyond it.

### 5.4 Rule the adapter must implement

For the proven variant only — G4, composer `_v` 10–16, bubble `_v` 3, macOS — each user bubble
is classified individually. The proof licenses per-record reconciliation, never a blanket
assumption:

1. Parse `richText` into the Lexical editor state and project it to plain text under the
   section 5.2 rule.
2. Reconcile the stored `text` against that projection. The bubble reconciles if the two are
   byte-exact, or equal after whitespace normalization, in either case after accounting for the
   `@` sigil that stored `text` carries on mention nodes and the projection does not.
3. A bubble that reconciles is `text_status = verbatim` and contributes analyzable text.
4. A bubble with no usable `richText` (the 3.5% bucket), or whose `text` does not reconcile with
   the projection, is `text_status = unknown` and contributes **no** analyzable text. It is
   still inventoried and counted, with a stable diagnostic code (`no_editor_state` /
   `text_projection_mismatch`).

The 3.5% no-editor-state bucket is uncorroborated, not disproven; spec 4.4 keeps `unknown`
records out of the default corpus, so they are quarantined rather than assumed verbatim.

Any bubble outside the proven variant — a different generation, a different `_v`, or a
non-macOS platform — is `text_status = unknown` and inventory-only regardless of how well it
reconciles. Reconciliation is a gate applied inside the proven variant, not a way to admit an
unproven one.

### 5.5 Mention handling

`@name` mention tokens are UI file references produced by an editor affordance, not authored
prose: the user selects a file from a picker rather than composing the token. They are stripped
from extracted text before analysis and never counted toward the project-spec 5.6 word
denominator. Stripping happens after reconciliation (reconciliation needs the sigil to explain
the residual).
Every utterance from which one or more mention tokens were removed carries the content flag
`mention_stripped`.

### 5.6 Contamination warning (the dominant remaining risk)

Rawness is now settled for the tested variant; *authorship* is not, and it is the harder
problem for this source. On the same corpus, stored prompts hold 1,840,474 words, of which
1,425,590 — **77.5%** — survive the shared authorship filter. Roughly a fifth of stored Cursor
prompt text is pasted terminal output, tracebacks, lint dumps, diffs, and code; a single prompt
exceeded 5,000 words and was almost entirely tool output.

Consequences:

- Cursor depends on the shared normalization layer (spec 4.5) more than any other V1 source. An
  authorship-filter regression that is invisible elsewhere is a corpus-poisoning event here.
- The line-based filter must be re-measured against this source before any stable promotion, and
  the surviving-word share must be reported per run so a drift in that ratio is visible.
- Pasted material is not structurally marked in the store, so length-threshold heuristics
  (section 6.2, `possible_paste`) remain advisory only; they do not substitute for the shared
  filter.

### 5.7 Input-method provenance (still unproven)

Cursor 2.0 (2025-10) added a native Voice Mode that batch-transcribes speech directly into the
agent prompt field, upgraded again in 3.1 (E7, vendor changelog: strong for existence). No
per-bubble field distinguishing typed, dictated, or pasted input was found anywhere in the
bubble, composer, or `ItemTable` key space of the tested store (E1; caveat: the tested store
contains no known voice-mode usage, so a marker appearing only on dictated bubbles cannot be
ruled out — unresolved question 1). Whether the built-in STT output is raw or cleaned is
unknown.

E11 does not touch this question: a dictated prompt reaches the editor state and is then stored
verbatim from it, so a verbatim reconciliation says nothing about what happened upstream of the
prompt box.

Therefore `modality` stays `written` under the project-spec 5.5 operational convention (coding-agent
text not positively matched to a raw voice source is `written`). Some `written` Cursor text may
in fact be machine-transcribed; this undisclosed contamination risk, together with the
single-machine scope of E11, is why the adapter ships as beta and is never auto-selected.

### 5.8 Everything not proven

All other generations (G1, G2, G3, G5), all `_v` values outside the proven ranges, and every
platform without its own completed structural verification (Windows, Linux, WSL, remote/SSH):
rawness `unknown` → inventory only, zero analyzable text, fail closed (spec 4.7). Equivalence is
never inferred from the macOS result, however plausible the shared Electron codebase makes it.

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

### 6.3 Per-bubble rawness gate

Inclusion (6.2) decides whether a bubble is a candidate; the section 5.4 gate then decides
whether that candidate carries analyzable text. Both must be applied, in this order:

1. The instance is the proven variant (macOS, G4, composer `_v` 10–16, bubble `_v` 3). If not,
   every bubble is `text_status = unknown`, inventory only — the gate below is not even run.
2. `richText` is present and parses into a usable Lexical editor state. If not: `text_status =
   unknown`, no analyzable text, count `no_editor_state`.
3. The section 5.2 projection of that editor state reconciles with the stored `text` —
   byte-exact or after whitespace normalization, in both cases after accounting for the `@`
   sigil on mention nodes. If it reconciles: `text_status = verbatim`, the bubble contributes
   analyzable text. If it does not: `text_status = unknown`, no analyzable text, count
   `text_projection_mismatch`.

A per-instance mismatch rate far above the observed ~0% post-sigil residual is a schema-drift
signal: above a configured threshold the instance falls to `unsupported_schema` rather than
silently emitting a shrinking corpus.

Text is taken only from `text`; the projection is used to qualify it, never as the extracted
value. Images, attachments, context fields, and tool data are never extracted. Mention tokens
are stripped from the extracted text per section 5.5. Per-utterance metadata: bubble UUID
(utterance ID basis), composer UUID (session basis), `createdAt` timestamp (nullable), modality
`written`, text status per the gate above, authorship basis `explicit_user_role_type1`.

Content flags:

- `mention_stripped` — one or more `@name` mention tokens were removed (section 5.5).
- `possible_paste` — above a configured length threshold, mirroring the `claude_code` adapter.
  Pasted text is not structurally marked, so this flag is advisory; the shared normalization
  layer removes copied/pasted/code-like material (spec 4.5), and per section 5.6 that layer
  carries more weight for this source than for any other.

### 6.4 Credential safety

The adapter never opens denylisted keys or files (section 3). Secrets typed into prompts are
handled downstream by the privacy pipeline.

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
   per workspace group. Apply section 6 rules identically to the later extraction, including
   the 6.3 rawness gate, so discovery counts reflect analyzable text and not merely present
   text. Report the gate's outcome as inventory counters (`verbatim`, `no_editor_state`,
   `text_projection_mismatch`) so a user can see how much of a store is usable before selecting
   it. Never print, log, or return text.
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
`written`, a source-path hash of the canonical original path, and text status assigned per
bubble by the section 6.3 gate. Only bubbles that reconcile carry `verbatim` and analyzable
text; `unknown` bubbles are emitted for counting with no analyzable text, so they cannot enter
the default corpus (spec 4.4) and cannot inflate the project-spec 5.6 denominator. Mention
tokens are stripped and flagged (section 5.5) after the gate has run.

### 8.4 verify()

Adapter-specific deterministic checks:

- Every utterance maps to a snapshot bubble row passing section 6.
- Every utterance marked `verbatim` re-reconciles against its `richText` projection on
  re-computation (section 5.4). A `verbatim` utterance that does not re-reconcile is a hard
  failure, not a tolerance: it raises `richtext_mismatch` and fails the instance closed.
- No utterance marked `unknown` contributes analyzable text or words to the denominator.
- The gate's counters sum to the candidate bubble count (`verbatim` + `no_editor_state` +
  `text_projection_mismatch`), and the per-instance mismatch share is recorded in the run
  manifest so drift away from the E11 baseline is visible across runs.
- No extracted text retains an `@` mention token (section 5.5 stripping is complete).
- The surviving-word share after the shared authorship filter is recorded per instance and
  compared against the E11 baseline of 77.5% (section 5.6); a large unexplained drop is
  reported, not silently accepted.
- No denylisted key or file appears in the opened-path/key audit; counts are internally
  consistent.
- No utterance text begins with a known injected wrapper tag (`<user_query`, `<attached`,
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
- User bubble without usable `richText`: `no_editor_state`; the bubble is inventoried, marked
  `text_status = unknown`, and contributes no analyzable text. Expected on ~3.5% of bubbles
  (E11); a much higher share is a drift signal.
- User bubble whose `text` does not reconcile with its editor-state projection:
  `text_projection_mismatch`; same treatment. Above the configured per-instance threshold the
  instance falls to `unsupported_schema` — the store no longer behaves like the proven variant.
- Locked database, permission errors, unreadable root: `inaccessible` with diagnostic code;
  bounded retries only.
- Unknown future keys, key prefixes, or JSON fields: ignored and counted, never guessed at.
- G1/G2/G3/G5 material and untested platform variants: always "detected, unsupported schema";
  inventory only, zero extracted text (spec 4.2, 4.7).

## 10. Unresolved questions and required behavior when evidence is insufficient

Resolved on 2026-08-08 and no longer open: *whether stored Cursor text is raw*. E11 settles it
for the proven variant (section 5). It settles nothing outside that variant, and questions 4, 6,
and 8 below carry what remains.

1. **Voice Mode marker:** whether a bubble created through native Voice Mode carries any
   distinguishing field (the tested store contains no known dictated bubble). E11 does not help:
   it proves faithful storage of the editor state, not the provenance of what reached the editor
   (section 5.7). Required behavior now: modality `written` by the project-spec 5.5 convention, beta
   stability, adapter never auto-selected. Required follow-up before any stable promotion:
   generate dictated bubbles on a test install and re-inspect; if a marker exists, dictated
   bubbles must be excluded until the STT raw-vs-cleaned question is settled; if none exists, the
   limitation stays in the compatibility matrix.
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
8. **Scope of the rawness proof (E11):** one user, one machine, macOS, one application
   generation, composer `_v` 10–16, bubble `_v` 3. Whether the same faithful serialization holds
   on Windows, on Linux, or in a future generation is unproven and must not be inferred.
   Behavior: the section 6.3 gate runs only inside the proven variant; every other variant is
   `text_status = unknown` and inventory-only. Required follow-up before stable promotion:
   repeat the section 5.2 projection comparison on at least one real installation per claimed
   platform, and re-run it after any observed `_v` change; report the four-way comparison shares
   in the compatibility matrix (spec 13.2).
9. **Behavior of the shared authorship filter on Cursor text:** only 77.5% of stored prompt
   words survive it, and the filter's precision and recall on pasted terminal output,
   tracebacks, and lint dumps have not been measured against this source specifically.
   Behavior: rely on the shared filter, flag `possible_paste`, and report the surviving-word
   share per run. Required follow-up before stable promotion: re-measure the line-based filter
   against a Cursor corpus and record the result; an unmeasured filter is not an acceptable
   basis for promoting the source that depends on it most (section 5.6).
10. **Mention-node subtypes:** the tested corpus contains 9,976 `mention` nodes, treated
    uniformly as UI file references and stripped. Whether Cursor emits other mention kinds
    (documentation, symbol, or command references) whose display names could carry authored
    words is unverified. Behavior: strip all mention nodes and flag `mention_stripped` — this
    fails closed by discarding text rather than admitting non-authored tokens, at the cost of a
    small denominator loss.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/cursor/<variant>/` with a `fixture.json` metadata file. All
content is synthetic; secret-looking values are unmistakably fake (for example
`sk-FAKEFAKEFAKE0000`). SQLite fixtures are generated by a checked-in builder script so no
binary blobs need hand maintenance.

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-g4` | Global DB: composers `_v` 10 and 16, bubbles `_v` 3 (user with `text`+`richText`, assistant with `codeBlocks`/`toolFormerData`), headers in order; workspace DB + `workspace.json` index | exact kept-utterance set, order from headers, workspace grouping, assistant/tool exclusion, every kept utterance `text_status = verbatim` |
| `rawness-gate` | Four synthetic user bubbles reproducing the E11 buckets: byte-identical `text`/`richText`; identical only after whitespace normalization; differing only by the `@` sigil on a mention node; and genuinely divergent text | first three are `verbatim` and contribute text; the fourth is `unknown`, contributes no text and no words, and counts `text_projection_mismatch` |
| `no-editor-state` | User bubbles with `richText` absent, empty, non-JSON, and structurally unusable | each is `unknown`, contributes no analyzable text, counts `no_editor_state`; the bubble still appears in inventory counts |
| `projection-rules` | `richText` exercising multi-paragraph roots, `tab` and `linebreak` nodes, an empty paragraph, and a paragraph containing only a mention | projection joins paragraphs with newlines and renders `tab`/`linebreak` literally; a naive projection that drops paragraph boundaries fails the test (regression guard for the 51.8% artifact) |
| `mention-stripping` | User bubbles with a leading mention, an inline mention, several mentions, and a mention-only prompt | mention tokens removed from extracted text, `mention_stripped` flag set, stripped tokens excluded from the word denominator, mention-only prompt yields zero analyzable words |
| `contamination` | User bubbles that are largely pasted terminal output, a traceback, and a lint dump, plus one clean prose prompt | the shared authorship filter's surviving-word share is computed and reported per instance; the clean prompt survives; the counters are asserted rather than the filter's exact output |
| `flags-excluded` | User bubbles with `isNudge: true`, `isQuickSearchQuery: true`, `skipRendering: true`, empty `text`; composer with `isBestOfNSubcomposer: true`; parent listing a `subComposerIds` child | each exclusion rule fires with its counter |
| `legacy-g3-embedded` | Composer with non-empty `conversationMap` | detected, unsupported schema; zero text; inventory line present |
| `legacy-g2-workspace` | Workspace `composer.composerData` with inline conversation content, no global rows | unsupported schema inventory, no extraction |
| `legacy-g1-aichat` | Workspace `workbench.panel.aichat.view.aichat.chatdata` with `tabs[]`/`bubbles[]` | unsupported schema inventory, no extraction |
| `empty` | Global DB with tables but no composer rows; missing global DB | found-empty vs not-found |
| `malformed` | Bubble rows with truncated JSON, non-object JSON; one composer over the 10% threshold; header referencing a missing bubble | thresholds, `missing_bubble`, no crash |
| `unsupported-version` | Bubbles `_v: 99`, composer `_v: 1` | version fail-closed counters |
| `wal-live` | DB with uncommitted `-wal` content | backup-API snapshot sees a consistent state |
| `denylist` | Store containing fake `cursorAuth/accessToken`, `secret://…`, `agentKv:` and `messageRequestContext:` rows, `state.vscdb.backup`, fake `~/.cursor/` tree with `mcp.json` and `agent-transcripts` | opened-path/key audit proves none were read |
| `richtext-mismatch` | Instance whose divergent-bubble share exceeds the per-instance threshold | `richtext_mismatch`; the instance falls to `unsupported_schema` rather than emitting a reduced corpus |
| `windows-paths` / `linux-paths` | Same `success-g4` content under `%APPDATA%\Cursor\...` and `.config/Cursor/...` layouts | path resolution per platform; **every bubble is `unknown` and contributes no text even when it reconciles perfectly** — the rawness proof does not travel across platforms; extraction stays gated until the platform smoke test is recorded |
| `wsl-hint` | Simulated `/mnt/c/Users/<fake>/AppData/Roaming/Cursor/...` presence | fail-closed WSL behavior plus hint diagnostic |

Platform matrix: the pytest suite runs on macOS, Linux, native Windows, and WSL runners.

Beta release requires the macOS smoke test (done during this research, outputs not committed)
and the rawness evidence E11 recorded in section 5. Beta means the adapter is never selected by
default; the user opts in and is told the limitations.

Stable promotion additionally requires all of:

- Windows and Linux smoke tests, each including its own section 5.2 projection comparison on a
  real installation (10.8) — the macOS result is not transferable.
- The Voice Mode investigation (10.1).
- A re-measurement of the shared authorship filter against a Cursor corpus (10.9).
- A published compatibility matrix (spec 13.2) naming the tested application version, storage
  fingerprint (`g4;composer_v=10-16;bubble_v=3`), operating-system environment, raw-field
  provenance, and the observed verbatim-equivalent share per platform.
- A research refresh.

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
