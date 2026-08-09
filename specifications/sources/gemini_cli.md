# Source specification: Google Gemini CLI (`gemini_cli`)

Status: reviewed source specification for the `gemini_cli` adapter (spec section 4.2 research gate).
Adapter ID: `gemini_cli`. Stability target: stable (after release gates pass); the shipped
adapter is beta until a real-installation smoke test runs.
Research basis: vendor source code (google-gemini/gemini-cli, TypeScript), vendor
documentation, vendor blog, and maintainer PR/issue threads. Web evidence only: Gemini CLI
is not installed on the reference machine, so no claim in this document rests on local
observation. The private research log with per-claim URLs, dates, and evidence strengths is
`temp/findings/gemini_cli-source-research.md` (evidence IDs E1–E21 cited below).
Access date for all cited evidence: 2026-08-08.

Researched application generation: main branch as of 2026-08 (post-v0.39) plus tagged
v0.11.0 for the legacy generation. Earliest generation this adapter supports: automatic
session recording introduced ~August 2025 (0.2.x-era builds; E20). Older installs persist
no sessions (only `/chat save` checkpoints and `logs.json`), and those files are detected
but not extracted (sections 3.4, 10).

Per spec section 4.7, this adapter supports the JSON and JSONL session generations and
ignores thoughts, tool calls/results, and inline media.

## 1. Platform status summary

| Platform | Status | Storage root |
|---|---|---|
| macOS | Supported, needs smoke test | `~/.gemini/` (E2, strong) |
| Linux (native) | Supported, needs smoke test | `~/.gemini/` (E2, strong) |
| Windows (native) | Supported, needs fixture + smoke test | `%USERPROFILE%\.gemini\` (E2 code + E10 vendor docs, strong) |
| WSL | Supported for the WSL-side store; Windows-host mount as a second, untested instance | WSL-side `~/.gemini/`; optional `/mnt/<drive>/Users/<user>/.gemini/` (inference from plain-file format, moderate) |

- The root is `homedir()/.gemini` on every platform; there is no roaming (`%APPDATA%`),
  sandbox, or container variant (E2, strong). Vendor docs confirm the Windows form
  `C:\Users\<user>\.gemini\tmp\<project_hash>\` (E10, strong).
- When `GEMINI_CLI_HOME` is set, the CLI treats it as the home directory and the effective
  root is `$GEMINI_CLI_HOME/.gemini` (E15, moderate-strong). Discovery checks it first.
- When no home directory resolves, the CLI falls back to `<os tmpdir>/.gemini` (E2). V1
  does not discover the tmpdir fallback (ephemeral, cannot be attributed to the user).
- All session data is plain JSON/JSONL text files. No SQLite, WAL, compression, or
  encryption anywhere in the session store, so spec section 4.6 database rules do not
  apply. Snapshotting is a plain file copy (section 8).
- WSL: a WSL-side install is a Linux install with its own `~/.gemini`. A Windows-host
  install seen through `/mnt/<drive>` is a separate dataset of plain files; discovery may
  report it as a second instance (checked only at the fixed
  `<profile>/.gemini/tmp` layout, never by walking profiles). It is never merged with the
  WSL-side instance. This variant is untested; the WSL release gate requires a smoke test
  on a real dual setup before it ships enabled.

## 2. Storage locations

### 2.1 Root resolution

1. If `GEMINI_CLI_HOME` is set and `$GEMINI_CLI_HOME/.gemini` exists, that is the root.
2. Otherwise `~/.gemini` (`%USERPROFILE%\.gemini` on native Windows).

Discovery never creates the root.

### 2.2 Project directories under `tmp/`

Per-project state lives in `<root>/tmp/<project-identifier>/`. Two identifier generations
exist (E1, E16):

- Hash generation: `<project-identifier>` = SHA-256 hex (64 lowercase hex chars) of the
  absolute project root path.
- Slug generation (present by 0.33.0, ~2026-02): `<project-identifier>` = a lowercase
  `[a-z0-9-]+` slug derived from the project directory basename, registered in
  `<root>/projects.json` and marked by a `.project_root` file inside the directory. Old
  hash directories are not renamed; both forms coexist.

Discovery treats every `tmp/<dir>/` containing a `chats/` subdirectory as one source
instance, regardless of identifier form. Directory names are never returned to the agent
(a slug can reveal a private project name); instances get opaque labels (`Gemini CLI 1`).

### 2.3 Files the adapter may open (allowlist)

```text
<root>/tmp/<project-identifier>/chats/session-*.jsonl   current sessions (JSONL generation)
<root>/tmp/<project-identifier>/chats/session-*.json    legacy sessions (monolithic JSON generation)
<root>/tmp/<project-identifier>/chats/*.json[l]         other top-level chat files: opened, then
                                                        schema-gated (section 3); kept only if they
                                                        parse as a supported session shape
```

Nothing else is ever opened. Subdirectories of `chats/` are subagent session trees
(`chats/<sanitized-parent-session-id>/*.jsonl`, E4) and are never opened or descended into:
their "user" records are prompts composed by the parent agent, not by the human.

### 2.4 Files that must NEVER be opened

Denylist, in addition to the allowlist above (E2, E9, E10, E11, E16, E17):

- `<root>/oauth_creds.json` — OAuth tokens. Never opened, never statted into output.
- `<root>/google_accounts.json`, `<root>/mcp-oauth-tokens.json`,
  `<root>/a2a-oauth-tokens.json`, `<root>/installation_id` — accounts, tokens, device ID.
- `<root>/settings.json`, `<root>/keybindings.json`, trusted-folders file — config; the
  adapter never reads settings, so a user-configured retention or recording policy is
  observed only through what exists on disk.
- `<root>/projects.json` — maps real project paths to slugs. Not needed; never opened.
- `<root>/GEMINI.md`, `<root>/commands/`, `<root>/skills/`, `<root>/agents/`,
  `<root>/policies/`, `<root>/extensions/` — instruction/config trees.
- `<root>/history/` — shadow Git repositories holding file-content snapshots for
  checkpointing (E11). High privacy risk; never opened.
- `<root>/tmp/<id>/logs.json` — raw user prompt log (`{sessionId, messageId, timestamp,
  type: "user", message}` entries, E9). Duplicates session text with no conversation
  structure; V1 never opens it (section 10.4).
- `<root>/tmp/<id>/shell_history` — typed shell commands.
- `<root>/tmp/<id>/checkpoints/` and `<root>/tmp/<id>/checkpoint-*.json` — `/chat save`
  and file-checkpointing artifacts (`{history: Content[], authType}`, E9, E11). They embed
  the injected `<session_context>` environment message and tool traffic as user-role API
  content, and duplicate session files; V1 detects their presence for the fingerprint but
  never opens them (section 3.4).
- `<root>/tmp/<id>/plans/`, `tasks/`, `memory/`, `otel/`, `*.log`, and every other file or
  directory under the root: default-deny. Unknown names are ignored silently and never
  returned to the agent.

## 3. Record schema by generation

`listProjectChatFiles` in the vendor code reads both `.json` and `.jsonl` chat files
(E2), so a real store may contain both generations side by side.

### 3.1 Generation J2 — JSONL sessions (v0.39.0+, released 2026-04-23; E4, E5, E6, strong)

File: `chats/session-<YYYY-MM-DDTHH-MM>-<sessionid8>.jsonl` (timestamp = first 16 chars of
ISO time with `:` → `-`; suffix = first 8 chars of the sanitized session UUID). One JSON
object per line, append-only. Line kinds are discriminated structurally, exactly as the
vendor reader does (E4):

| Test (in this order) | Line kind | Adapter handling |
|---|---|---|
| has string `$rewindTo` | rewind marker | Skip (section 6.4) |
| has object `$set` | metadata update (`$set: Partial<ConversationRecord>`) | Read `$set.sessionId`/`summary` updates only for metadata; never text |
| has string `id` | message record | Classify by `type` (below) |
| has string `sessionId` and string `projectHash` | session metadata (`PartialMetadataRecord`) | Read metadata (first line of a healthy file) |
| anything else | unknown | Skip; count `skipped_unknown_line` |

Session metadata fields: `sessionId`, `projectHash` (SHA-256 of the project root, kept
local only), `startTime`, `lastUpdated`, `summary?`, `memoryScratchpad?`, `directories?`,
`kind?: 'main' | 'subagent'` (E5).

Message record = `{id, timestamp, content, displayContent?}` plus one of (E5):

- `type: 'user' | 'info' | 'error' | 'warning'` — no other fields; or
- `type: 'gemini'` — plus `toolCalls?` (`ToolCallRecord[]`), `thoughts?`
  (`{subject, description, timestamp}[]`), `tokens?`, `model?`.

`content` and `displayContent` are `PartListUnion` from `@google/genai`: a string, a
`Part` object, or an array of strings/parts. Relevant `Part` members: `text`,
`inlineData` (inline media), `fileData`, `functionCall`, `functionResponse`,
`executableCode`, `codeExecutionResult`, `thought`, `thoughtSignature` (moderate; SDK
types). Extraction touches only string items and `text` fields (section 5).

### 3.2 Generation J1 — monolithic JSON sessions (~0.2.x 2025-08 through 0.38; E7, E20, strong)

File: `chats/session-<timestamp>-<sessionid8>.json`. A single pretty-printed JSON object,
rewritten whole on every turn:

```json
{
  "sessionId": "...", "projectHash": "...",
  "startTime": "...", "lastUpdated": "...",
  "messages": [ { "id": "...", "timestamp": "...", "type": "user", "content": [ ... ] }, ... ]
}
```

Message records have the same shapes as J2 (the `info`/`error`/`warning` types appear
mid-generation, E21; `displayContent` appears late in the generation). Early J1 files lack
both; feature-detect per record, never by version. Optional top-level `summary`,
`memoryScratchpad`, `directories`, `kind` keys may or may not exist.

Migration: on resume under v0.39+, the CLI rewrites a `.json` session as `.jsonl` (same
name + `l`, E4). Whether the `.json` original is always removed is unverified; the adapter
dedups migrated pairs (section 6.3).

### 3.3 Schema fingerprint

Per instance: which generations are present (`json-v1`, `jsonl-v1`, `mixed`), presence of
`displayContent`, `info`/`error`/`warning` types, `kind` metadata, subagent
subdirectories, and checkpoint files (names only). Reported as the storage-variant
fingerprint; no version string exists inside session files, and no parsing decision may
branch on a CLI version number.

### 3.4 Detected but never extracted (V1)

- `checkpoint-<tag>.json` (`/chat save`) and `checkpoints/*.json` (file checkpointing):
  Gemini API `Content[]` histories (`role: 'user' | 'model'`). Their user-role entries mix
  human text with the injected environment context (which is a user-role `Content`
  beginning `This is the Gemini CLI. We are setting up the context` inside
  `<session_context>` tags, E13), at-command file expansions, and `functionResponse` tool
  results. Post-2025-08 checkpoints duplicate session files. V1 never opens them; their
  presence is noted in the fingerprint. Pre-2025-08 installs whose only history is
  checkpoints report "found" with zero supported sessions.
- `logs.json`: user-prompt duplicate log; never opened (sections 2.4, 10.4).

## 4. Provenance of text fields

- `type: 'user'` message `content`: the parts sent to the model for a user turn. For a
  plain typed prompt this is the typed text, verbatim — Gemini CLI does not rewrite,
  spellcheck, or enhance prompt text before persisting (no such code path exists in the
  recording chain, E4/E12). For `@file` references and custom slash commands, `content` is
  the expanded form (file contents appended after a marker part; command template
  expanded), i.e. partially non-authored (E18).
- `displayContent` (when present): what the UI displayed for that turn — the pre-expansion
  typed form. It is persisted only when it differs from `content` (E12). Its presence is
  therefore a reliable "content was transformed" signal.
- Synthetic user records: tool/function responses are recorded as `type: 'user'` messages
  whose parts are `functionResponse` objects; no persisted flag distinguishes them (E12).
  Structural part filtering is the only defense.
- `type: 'gemini'` content, `thoughts`, `toolCalls`, `tokens`: model- or tool-generated.
- `type: 'info' | 'error' | 'warning'`: UI notification text (E21). Generated.
- Modality: `written`. Gemini CLI has no built-in ASR path; dictated text arrives via an
  external tool and is handled by cross-source deduplication (spec 4.8). Text status of
  kept text: `verbatim`.

## 5. Inclusion and exclusion rules

### 5.1 Session eligibility (whole file)

Exclude the entire file (no candidate text) when any holds:

1. The file does not parse as a supported J1 or J2 shape (section 9).
2. Session metadata (J2 first line, `$set` update, or J1 top level) carries
   `kind: 'subagent'`.
3. The file lives in a subdirectory of `chats/` (never opened at all; defense in depth).

### 5.2 Candidate user text (per message record)

A message record contributes candidate text only if all hold:

1. `type == "user"`.
2. No part of `content` is or contains `functionCall`, `functionResponse`,
   `executableCode`, or `codeExecutionResult` (excludes synthetic tool-response records
   entirely — one such part poisons the whole record).
3. Source parts: if `displayContent` is present, extract from `displayContent` and set
   `content_flags: ["expanded_content_present"]`; otherwise extract from `content`.
4. From the source parts, take string items and `text` fields of parts, in order. Ignore
   `inlineData`, `fileData`, and any part with a truthy `thought` flag. Parts with
   unrecognized keys and no `text`: ignore the part and flag
   `content_flags: ["unknown_part"]`.
5. At-command expansion cut (J1 or any record without `displayContent`): if a text part
   consists of a `---`-delimited header line whose text matches the referenced-files
   marker (`/^-{2,}.*(referenced files|Content from).*-{0,}$/i` applied to a single line,
   feature-detected; E18), drop that part and every later part, and flag
   `content_flags: ["reference_expansion_trimmed"]`. If a single text part embeds the
   marker mid-string, keep only the text before the marker line.
6. Joined text, trimmed, is non-empty.
7. Wrapper filters on the trimmed text:
   - Starts with `/` or `!` → exclude (slash command or shell passthrough, not prose).
   - Starts with `<session_context>` or with `This is the Gemini CLI. We are setting up
     the context` → exclude (injected environment context, E13; defensive — normally
     absent from session files).
   - Starts with another `<tag>` matching a known injected wrapper set (maintained in the
     adapter) → exclude; unknown leading tags → keep with
     `content_flags: ["unknown_wrapper"]`.

Always excluded, never text sources: `gemini`, `info`, `error`, `warning` records;
`thoughts`, `toolCalls`, `tokens`, `model` fields; `$set` and `$rewindTo` lines; session
metadata `summary` and `memoryScratchpad` (both are generated summaries); `directories`
(paths). Per spec 4.7: thoughts, tool calls/results, and inline media are ignored.

Extraction metadata per kept utterance: message `id` (basis of the stable utterance ID),
session ID, message `timestamp`, modality `written`, text status `verbatim`, authorship
basis `explicit_user_role` (plus `display_content_preferred` when rule 3 used
`displayContent`).

Credential hygiene: the adapter never opens denylisted files (2.4). Secrets typed inside
prompts are handled by the downstream normalization and privacy stages.

## 6. Sessions, timestamps, deduplication

### 6.1 Session identity

`sessionId` from session metadata (J2 first line / J1 top level). Fallback when metadata
is missing but message records parse: the 8-char ID fragment plus timestamp from the
filename, reported with a `session_meta_missing` diagnostic. Session IDs are stored only
as salted local hashes in normalized utterances.

### 6.2 Timestamps

Per-message `timestamp` (ISO 8601) is the utterance timestamp; fallback: metadata
`startTime`; if both missing, the utterance is undated (`None`, never zero). Instance
range = min/max over kept utterances. Retention honesty: Gemini CLI deletes sessions older
than 30 days by default (`general.sessionRetention`, enabled by default with
`maxAge: "30d"`; E3, E14). A short observed history is normal and must not be reported as
an error or as "no long-term usage".

### 6.3 Within-adapter deduplication

1. Migrated pairs: a `session-X.json` and `session-X.jsonl` (or any two files) sharing one
   `sessionId` are one session; the `.jsonl` copy is canonical, the `.json` copy is
   dropped with a `migrated_duplicate` note.
2. Repeated message `id` within one file (J2 append semantics may rewrite a message):
   the last occurrence wins; text from earlier occurrences is not extracted separately.
3. Rewind (`$rewindTo`): messages after the rewind target remain user-authored English.
   V1 keeps them for analysis (they were genuinely produced); re-sent edited prompts after
   a rewind are near-duplicates handled by the normalizer's fuzzy dedup.
4. Resumed sessions append to the same session identity; no duplication arises.
5. WSL dual instances (WSL-side plus Windows-host mount): distinct stores in the normal
   case; exact-hash plus session-ID dedup collapses any overlap.
6. Cross-source duplicates (dictation pasted into Gemini CLI) are the shared normalizer's
   job (spec 4.8).

## 7. Safe discovery behavior

`discover()` (read-only, local, no network, no model):

1. Resolve roots per 2.1 (plus the WSL mount probe per section 1). Missing root →
   `not_found`.
2. Enumerate `tmp/*/` directories containing `chats/`. Root present but no such directory
   → `found` with zero instances (installed, no recorded sessions — normal for pre-2025-08
   versions or wiped retention).
3. Each qualifying project directory is one instance with an opaque label. Never return
   the directory name (hash or slug), any path, or `projectHash`.
4. For each allowlisted chat file: stream-parse (J2) or parse whole (J1, bounded by a size
   cap; files above the cap are counted `oversize_skipped`, not read into memory), apply
   section 5, and accumulate candidate message/word/byte counts and min/max timestamps.
   Tolerate blank lines and a truncated final JSONL line (live sessions append;
   `truncated_tail` note, not an error). Never print, log, or return text.
5. Report the section 3.3 fingerprint, estimated records, date range, and stability per
   instance as `InstanceInventorySummary` only.

## 8. Snapshot, extraction, verification

- `snapshot()`: byte copy of selected chat files into
  `<repository>/runtime/runs/<run-id>/snapshots/gemini_cli/<instance-hash>/`, preserving
  relative filenames, after the spec 3.6 preflight (containment, symlink, git-ignore,
  cloud-sync checks). Plain append-only/rewrite files need no locking; a J1 file caught
  mid-rewrite that fails to parse in the snapshot is reported `malformed_file` for that
  run, never retried against the live store in the same pass. Snapshot copies are written
  `0600` in `0700` directories. The snapshot manifest records source sizes, mtimes, and
  SHA-256 hashes. The adapter never writes, locks, renames, or deletes anything under the
  Gemini root.
- `extract()`: runs only against the snapshot; emits `NormalizedUtterance` records per
  spec 4.4 (modality `written`, text status `verbatim`, source-path hash of the canonical
  original path — never the path).
- `verify()`: every utterance maps to a snapshot record passing section 5; no utterance
  text begins with a known wrapper or marker; opened-path audit shows only allowlisted
  files; counts internally consistent; migrated-pair dedup left no orphan references.

## 9. Failure behavior when evidence is insufficient

- JSONL file whose first parseable line is not session metadata: process message lines if
  they discriminate cleanly, with `session_meta_missing`; if no line matches any known
  kind, the file is `detected, unsupported schema`.
- J1 file that parses as JSON but lacks `sessionId` + `messages` array: `detected,
  unsupported schema` for that file.
- Malformed line in J2 (non-final): skip and count; over 10% malformed non-empty lines →
  file `unsupported_schema`, no extraction. Truncated final line: drop silently.
- J1 whole-file parse failure: `malformed_file`, zero candidates, other files unaffected.
- Unknown message `type` values: exclude the record, count `unknown_message_type`; if such
  records dominate a file (>50%), the file is `unsupported_schema`.
- A `user` record whose `content` is neither string nor array nor part-object: excluded,
  counted `unrecognized_user_record`.
- Instance where every file is unsupported: instance `detected, unsupported schema` with
  diagnostic code; not selectable; no guessing.
- Permission errors on root or `tmp/`: instance `inaccessible` with diagnostic code; never
  retried with elevated privileges.

## 10. Unresolved questions and required behavior

1. Exact first release tag carrying automatic session recording (evidence pins it to
   ~2025-08 commits and verifies v0.11.0; E20/E7). Behavior: none required — detection is
   structural; older installs simply have no `chats/` files.
2. Whether `.json` originals are deleted after JSONL migration. Behavior: dedup by
   `sessionId` (6.3) regardless.
3. Exact `REFERENCE_CONTENT_START`/`END` marker strings (E18, moderate). Behavior: the
   marker regex in 5.2.5 is feature-detected; when no marker matches but `displayContent`
   is absent and the record has multiple text parts, flag
   `content_flags: ["multipart_no_display"]` so normalization can quarantine suspected
   expansions. Verify exact strings against a fixture before release.
4. `displayContent` provenance beyond the observed call path (is it always the typed
   form?). Evidence: E12, moderate. Behavior: prefer it with the
   `display_content_preferred` basis and `expanded_content_present` flag; if the release
   smoke test shows any non-typed `displayContent`, downgrade rule 5.2.3 to exclusion.
5. Non-interactive runs (`gemini -p "..."`) and their session files: recording appears to
   apply (nonInteractiveCli migration PR), unverified. Behavior: structural rules cover
   them; no special casing.
6. `/resume save <tag>` named branch points (E3): on-disk artifact shape unverified.
   Behavior: whatever lands in `chats/*.json[l]` is schema-gated by section 3; anything
   else is never opened.
7. Windows-host-store-from-WSL: DrvFS mtime/permission behavior unverified. Behavior: the
   variant ships only after a real dual-setup smoke test; until then it is reported as a
   detected instance with `stability: experimental` and contributes no analyzable text.
8. Whether any settings key disables session recording entirely. The adapter never reads
   settings; an installed CLI with an empty `chats/` tree is `found, empty`, never
   `not_found`.
9. Telemetry output locations under the project tmp dir (E17). Behavior: default-deny
   covers them; no telemetry file is ever opened.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/gemini_cli/<variant>/` with `fixture.json` metadata. All
content is synthetic; secret-looking values are unmistakably fake (e.g.
`ya29.FAKEFAKEFAKE0000`). Filenames follow the real `session-<ts>-<id8>.json[l]` patterns
with fixed synthetic UUIDs.

| Variant | Contents | Expected result |
|---|---|---|
| `success-jsonl` | J2 file: metadata line (`kind: 'main'`), plain user messages (string and parts forms), a user record with `displayContent` differing from expanded `content`, gemini records with `thoughts`/`toolCalls`/`tokens`, `info`/`error`/`warning` records, a `$set` summary update, a `$rewindTo` line, a synthetic user record with `functionResponse` parts, an `inlineData` image part | exact kept-utterance set; displayContent preferred; tool-response and media excluded; rewind ignored |
| `success-json-legacy` | J1 monolithic file: no `displayContent`, an at-command message with reference-marker parts, plain prompts, gemini/thoughts records | marker cut applied; only typed prose kept |
| `success-mixed-instance` | one instance containing both a J1 and a J2 file with different sessionIds | `mixed` fingerprint; both extracted |
| `migration-pair` | `session-X.json` and `session-X.jsonl` sharing a sessionId with overlapping messages | `.jsonl` canonical; zero duplicate utterances |
| `subagent` | top-level main session plus `chats/<parent>/agent.jsonl` subtree and a top-level file with `kind: 'subagent'` | subtree never opened; subagent-kind file excluded whole |
| `slug-and-hash-dirs` | two instances: `tmp/<64-hex>/chats/` and `tmp/my-project/chats/` with `.project_root` | both discovered; labels opaque; no name returned |
| `empty` | root with `tmp/` but no `chats/`; instance with empty `chats/`; zero-byte `.jsonl` | found-empty states; no errors |
| `malformed` | truncated final JSONL line; mid-file garbage line; file over the 10% threshold; unparsable J1 file | tail dropped; thresholding; `malformed_file`; no crash |
| `unsupported` | JSONL of unknown line shapes; J1-like JSON without `messages`; checkpoint-style `{history: [...]}` file inside `chats/` | `detected, unsupported schema` per file; checkpoint shape never treated as a session |
| `wrapper-context` | user records starting with `/cmd`, `!ls`, `<session_context>`, and the environment-context sentence | all excluded; plain prompts in the same file kept |
| `denylist` | root with fake `oauth_creds.json`, `google_accounts.json`, `projects.json`, `logs.json`, `shell_history`, `checkpoint-tag.json`, `checkpoints/`, `history/` shadow repo | opened-path audit proves none were read |
| `home-override` | populated root reachable only via `GEMINI_CLI_HOME` | override honored; default root ignored |
| `dual-root-wsl` | WSL-style root plus `/mnt/c/Users/<fake>/.gemini`-style root | two instances, separate labels, no merge, mount instance experimental |

Platform matrix: the full suite runs on macOS, Linux, native Windows, and WSL runners;
path handling and permission-error fixtures run per platform. Release gates additionally
require a private smoke test against at least one real installation per claimed
platform/storage variant (spec section 4.2 and the adapter authoring guide); smoke-test
content is never committed. Deterministic assertions per success variant: candidate
counts, word counts, per-utterance text hashes, timestamps, fingerprints, and diagnostic
codes are golden-file asserted.

## 12. Reproducible read-only inspection commands

Structure-only commands; safe on a real installation. Placeholder paths only. They print
field names, types, and counts — never values.

```bash
ROOT="${GEMINI_CLI_HOME:+$GEMINI_CLI_HOME/.gemini}"; ROOT="${ROOT:-$HOME/.gemini}"

# Instance layout (names stay in the terminal; never paste them to an agent)
ls "$ROOT/tmp" | wc -l
find "$ROOT/tmp" -maxdepth 2 -name chats -type d | wc -l

# Generation census per instance (file extensions only)
find "$ROOT/tmp" -maxdepth 3 -path '*/chats/*' -name 'session-*' \
  | sed 's/.*\.//' | sort | uniq -c

# JSONL line-kind census of one file (no content printed)
python3 -c 'import json,sys,collections
c=collections.Counter()
for l in open(sys.argv[1], errors="replace"):
    l=l.strip()
    if not l: continue
    try: o=json.loads(l)
    except Exception: c["<bad>"]+=1; continue
    k="$rewindTo" if "$rewindTo" in o else "$set" if "$set" in o else \
      "message:"+str(o.get("type")) if "id" in o else \
      "meta" if "sessionId" in o and "projectHash" in o else "unknown"
    c[k]+=1
print(c.most_common())' <one session-*.jsonl file>

# Legacy JSON: message-type census and key set only
python3 -c 'import json,sys,collections
o=json.load(open(sys.argv[1], errors="replace"))
print(sorted(o.keys()))
print(collections.Counter(m.get("type") for m in o.get("messages", [])).most_common())
' <one session-*.json file>

# User-record shape census (booleans only: has displayContent, part kinds)
python3 -c 'import json,sys,collections
c=collections.Counter()
for l in open(sys.argv[1], errors="replace"):
    try: o=json.loads(l)
    except Exception: continue
    if o.get("type")!="user" or "id" not in o: continue
    parts=o.get("content"); parts=parts if isinstance(parts,list) else [parts]
    kinds=tuple(sorted({("str" if isinstance(p,str) else ",".join(sorted(p.keys())) if isinstance(p,dict) else "?") for p in parts}))
    c[("displayContent" in o, kinds)]+=1
print(c.most_common())' <one session-*.jsonl file>
```

Never run `cat`, `jq .`, or any command that prints record values against a real store.
