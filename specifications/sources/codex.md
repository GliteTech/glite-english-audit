# Source specification: OpenAI Codex CLI (`codex`)

Status: reviewed source specification for the `codex` adapter (spec section 4.2 research gate).
Adapter ID: `codex`. Stability: stable (after release gates pass).
Research basis: primary vendor source code and documentation plus labeled local
reverse engineering. The private research log with per-claim evidence lives in
`temp/findings/codex-source-research.md` during development. Every rule needed to implement or
review this adapter is stated in this file.

Tested application generation at research time: Codex CLI 0.146.x (2026-08). Earliest generation
this adapter supports: session files written by Codex CLI ~0.39.0 (2025-09) and later, which use
the wrapped `RolloutLine` format. Older bare-item files are detected but unsupported.

## 1. Platform status summary

| Platform | Status | Storage root |
|---|---|---|
| macOS | Supported | `$CODEX_HOME`, default `~/.codex/` |
| Linux (native) | Supported | `$CODEX_HOME`, default `~/.codex/` |
| Windows (native) | Supported | `%CODEX_HOME%`, default `%USERPROFILE%\.codex\` |
| WSL | Supported (two instance kinds, see 2.4) | WSL-side `~/.codex/`; optional Windows-host root via `/mnt/<drive>/Users/<user>/.codex/` |

Codex CLI runs natively on Windows (npm install under PowerShell; OpenAI documents a native
Windows install path). WSL is not required, but many users run Codex inside WSL. Both native
Windows and WSL are therefore real, separately tested storage environments for this adapter.

Session data is plain append-only JSONL. There is no SQLite, WAL, compression, or encryption in
the session store, so the section 4.6 database rules do not apply to this adapter. Snapshotting is
a plain file copy (see 8).

## 2. Storage locations

### 2.1 Root resolution

1. If the `CODEX_HOME` environment variable is set and names an existing directory, that is the
   root.
2. Otherwise the root is `~/.codex` (`%USERPROFILE%\.codex` on native Windows).

Discovery never creates the root and never follows a symlinked root outside the user profile
without recording it in the instance fingerprint.

### 2.2 Files and directories the adapter may open

Allowlist. The adapter opens nothing outside this list:

```text
<root>/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DD>T<hh-mm-ss>-<uuid>.jsonl   current sessions
<root>/archived_sessions/rollout-*.jsonl                                  archived sessions
```

- `sessions/` is sharded by UTC date directories: four-digit year, zero-padded month, zero-padded
  day. The filename timestamp uses hyphens instead of colons for filesystem safety. The trailing
  UUID is the session (thread) ID; recent versions use time-ordered UUIDv7.
- `archived_sessions/` is a flat directory with the same filenames. Files move there via the
  `/archive` command or `codex archive <id>` and move back via `codex unarchive`. Content format
  is identical to current sessions. Both directories are inventoried; archived sessions are
  eligible on the same terms as current ones.
- `<root>/history.jsonl` exists (one object per line: `session_id`, `ts` epoch seconds, `text`)
  and duplicates user message text across sessions for prompt recall. V1 does not read it: it
  adds no records that rollout files lack, has no per-line role/context markers, and would force
  extra deduplication. It stays out of the allowlist.

### 2.3 Files that must NEVER be opened

Deny listed explicitly, in addition to the allowlist above:

- `<root>/auth.json` — API keys and OAuth tokens. Never opened, never statted into output.
- `<root>/config.toml` — may reference provider settings and local policy. Never opened.
- Any other file or directory under the root, including `history.jsonl`, `*.sqlite*`, caches,
  logs, and vendor- or fork-specific files. Unknown files are ignored silently; their names are
  not returned to the agent.

### 2.4 Platform specifics

- macOS and native Linux: identical layout under `~/.codex`. No sandbox/container variant exists;
  Codex CLI is not distributed through the macOS App Store sandbox.
- Native Windows: identical layout under `%USERPROFILE%\.codex`. Paths in file content use
  Windows separators; the adapter treats path-like strings as opaque. No roaming variant:
  Codex writes to the profile directory, not `%APPDATA%`.
- WSL: two independent instance kinds.
  1. WSL-side install: `~/.codex` on the Linux filesystem. Primary, fully supported.
  2. Windows-host install visible from WSL: `/mnt/<drive>/Users/<user>/.codex`. Because session
     files are plain append-only JSONL (no database locking), a read-only copy through DrvFS is
     safe. Discovery may report this as a second, separate instance when the mount and directory
     exist. It is never merged with the WSL-side instance; cross-instance duplicates are handled
     by deduplication (see 7).
  Discovery enumerates candidate Windows user profiles under mounted drives only by checking for
  the fixed `<profile>/.codex/sessions` layout; it does not walk profile directories.

Each discovered root is one source instance with an opaque label (`Codex 1`, `Codex 2`).
Private usernames, drive letters, and paths are hashed, never returned to the agent.

## 3. Record schema

### 3.1 Line envelope (`RolloutLine`)

Since Codex ~0.36–0.39 (PR openai/codex#3380, merged 2025-09-09), every line is one JSON object:

```json
{"timestamp": "2026-01-15T10:00:00.123Z", "type": "<line type>", "payload": { ... }}
```

- `timestamp`: UTC ISO-8601 with millisecond precision and `Z` suffix.
- `type`: serde snake_case tag of the `RolloutItem` variant.
- `payload`: variant body.
- Recent versions may add an optional `ordinal` integer. Unknown extra envelope keys are
  tolerated and ignored.

Observed and vendor-confirmed line types:

| `type` | Persisted payload | Adapter handling |
|---|---|---|
| `session_meta` | Session metadata (see 3.2) | Read; first line of every supported file |
| `response_item` | Model-protocol item (see 3.3) | Read; one extraction channel |
| `event_msg` | UI event (see 3.4) | Read; other extraction channel |
| `turn_context` | cwd, model, approval and sandbox policy | Skip (context, not text) |
| `compacted` | Compaction marker; may embed `replacement_history` re-serialized messages | Skip entirely (dedup hazard, see 7) |
| `world_state` | Environment/capability snapshot (newer versions) | Skip |
| `inter_agent_communication` / `inter_agent_communication_metadata` | Multi-agent traffic (newer versions) | Skip |
| anything else | — | Skip line; count as `skipped_unknown_line` |

### 3.2 `session_meta` payload by generation

The payload key set has grown across versions. Keys the adapter reads:

| Key | Since | Use |
|---|---|---|
| `id` | always | Session/thread ID (UUID string) |
| `timestamp` | always | Session start, UTC ISO-8601 |
| `cli_version` | always | Detected application version for the instance report |
| `cwd` | always | Never returned; ignored |
| `originator` | always | Ignored (`codex_cli_rs`, `codex-tui`, `codex_vscode`, ...) |
| `instructions` (≤ ~0.4x) / `base_instructions` (later) | varies | Ignored (injected instructions, not user text) |
| `git` | optional | Never returned; ignored (branch, commit, repo URL) |
| `source` | ~0.45+ | Eligibility: string form (`"cli"`, `"vscode"`, ...) or object form; an object containing a `subagent` key marks an agent-spawned session (see 5) |
| `thread_source`, `parent_thread_id`, `forked_from_id`, `agent_nickname`, `agent_path`, `agent_role` | newer | Eligibility and lineage: any of these marks subagent or forked provenance |
| `history_mode` | newer | `"legacy"` or `"paginated"`; drives feature detection (see 6) |

Unknown keys are tolerated. A file whose first non-empty line is not a valid `session_meta`
`RolloutLine` is unsupported (see 9).

A file may contain more than one `session_meta` line (resume/fork appends). The first one is
authoritative for session identity; later ones are read only for eligibility flags, and the
session is excluded if any of them carries subagent provenance.

### 3.3 `response_item` payloads

serde-tagged by inner `type`. Relevant subtypes:

- `message`: `{"type": "message", "role": "user" | "assistant" | "developer" | "system", "content": [ ... ]}`.
  Content parts are objects with their own `type`: `input_text` (user/developer input, has
  `text`), `output_text` (assistant output), `input_image`, and possibly others. Only
  `role == "user"` with `input_text` parts can contain user-authored text — after the injection
  filters in section 5.
- `reasoning`: model reasoning (`summary`, `content`, `encrypted_content`). Excluded.
- `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`,
  `local_shell_call`, `web_search_call`, and similar: tool traffic. Excluded.
- `agent_message` and any other subtype: excluded.

### 3.4 `event_msg` payloads

serde-tagged by inner `type`. Relevant subtypes:

- `user_message`: `{"type": "user_message", "message": "<text>", ...}`. In older versions
  (~0.39–0.4x) it carries `kind`: `"plain"`, `"user_instructions"`, or `"environment_context"`.
  Newer versions omit `kind`. Newer versions may add `images`, `local_images`, `text_elements`,
  and similar keys; they are ignored.
- `agent_message`, `agent_reasoning`, `token_count`, `task_started`, `task_complete`,
  `turn_aborted`, `patch_apply_end`, `context_compacted`, `web_search_end`, and all other
  subtypes: excluded.

`event_msg` lines are written only when the session's history mode is legacy. Paginated-mode
files persist `item_completed` events instead. This is why event nesting must be
feature-detected per file (section 6).

### 3.5 Schema and version markers

Rollout files carry no explicit schema version. The adapter fingerprint is derived per file from:

1. First line parses and has `type == "session_meta"`.
2. `session_meta` payload key set (generation signal: `instructions` vs `base_instructions`,
   presence of `history_mode`, shape of `source`).
3. The set of observed line `type` values.
4. `cli_version` string (reported, not trusted for parsing decisions).

### 3.6 Migration history

| Generation | Approx. versions | Format | Adapter policy |
|---|---|---|---|
| Bare items | ≤ ~0.35 (before 2025-09-09) | First line bare `SessionMeta` object; later lines bare `ResponseItem` objects, some with a `record_type` key; no `type`/`payload` envelope | Detected, unsupported schema |
| Wrapped, legacy events | ~0.39 → current | `RolloutLine` envelope; `event_msg` `user_message` present; `kind` present early, absent later | Supported |
| Wrapped, paginated | newer 0.1xx builds when `history_mode: "paginated"` | No `event_msg` `user_message`; `response_item` channel only | Supported via channel B |
| Multi-agent / fork metadata | ~0.11x+ | `source` object form, `parent_thread_id`, `forked_from_id`, `world_state`, `compacted` with `replacement_history` | Supported with eligibility and dedup rules |

## 4. Provenance of text fields

- User-authored channel(s): text the user typed (or pasted) into the Codex composer. Codex does
  not rewrite, spellcheck, or "enhance" user input before persisting it. Text status:
  `verbatim`. Modality: `written` (Codex has no built-in ASR path; dictated text arrives through
  an external tool and is handled by cross-source deduplication).
- Injected, not user-authored: `<environment_context>` blocks, `<user_instructions>` blocks,
  AGENTS.md content, skill/plugin instruction expansions, compaction bridge summaries, and
  developer-role messages. These appear as user-role `response_item` messages or as instruction
  fields and must be excluded (section 5).
- Generated: assistant output, reasoning, tool calls and outputs.
- Unknown provenance: none required for V1; anything unrecognized is excluded, not quarantined
  as `unknown`.

## 5. Inclusion and exclusion rules

### 5.1 Session eligibility (whole file)

Exclude the entire file (no candidate text) when any holds:

1. First non-empty line is not a valid `session_meta` `RolloutLine` (unsupported, see 9).
2. Any `session_meta` payload in the file has: `source` as an object containing `subagent` (any
   nesting), or a non-null `parent_thread_id`, or non-null `agent_nickname`/`agent_path`/
   `agent_role`. These sessions are agent-spawned; their "user" messages are model- or
   orchestrator-composed prompts, not human text.
3. `history_mode` present with a value other than `legacy` or `paginated` (unknown future mode:
   unsupported schema for that file).

`forked_from_id` alone does not exclude a session (the fork may contain real user turns); it
flags the file for cross-file deduplication (section 7).

### 5.2 Candidate user text (per line)

Channel A — `event_msg` `user_message` (used when the file has such lines):

- Include `payload.message` when `kind` is absent or `kind == "plain"`.
- Exclude when `kind` is `"user_instructions"`, `"environment_context"`, or any other value.
- Apply the tag filter below regardless of `kind`, because newer versions drop `kind`.

Channel B — `response_item` `message` (used when the file has no Channel A lines):

- Include only `role == "user"`.
- Concatenate `content` parts with `type == "input_text"`; ignore `input_image` and other part
  types. A message with no `input_text` part yields no candidate.

Tag filter (both channels). Exclude the candidate when the trimmed text:

- starts with `<environment_context>`, `<user_instructions>`, `<ENVIRONMENT_CONTEXT>`, or
  `<USER_INSTRUCTIONS>`;
- starts with `<turn_context>` or `<permissions` (observed injected context wrappers);
- contains `<environment_context>` anywhere within its first 200 characters (wrapper variants);
- starts with `# AGENTS.md` or is an exact AGENTS-file expansion (first line `# AGENTS`);
- is empty after trimming.

Role/denylist summary (always excluded): assistant, developer, and system roles; reasoning;
all tool call/output items; all non-`user_message` events; `turn_context`, `world_state`,
`compacted`, and inter-agent lines; every `session_meta` instruction field.

Credential hygiene: the adapter never opens `auth.json` or `config.toml` (2.3). Text inside
included user messages may still contain secrets; that risk is handled by the downstream
normalization and privacy stages, not by the adapter.

### 5.3 Sessions, timestamps, IDs

- Session ID: `session_meta.payload.id` (fallback: UUID parsed from the filename when the two
  disagree, with a `session_id_mismatch` diagnostic). Stored only as a salted local hash in
  normalized utterances.
- Utterance timestamp: the `RolloutLine.timestamp` of the included line (UTC). Fallback when
  missing: `session_meta.payload.timestamp`; if both missing, the utterance has no timestamp.
- Period filtering uses the line timestamp; the date directory and filename are used only for
  fast pre-filtering during discovery (a file whose directory date lies outside the selected
  period by more than 2 days on both ends may be skipped without parsing).
- Utterance ID: deterministic hash of (adapter ID, session ID, line index, text hash) per the
  shared hashing rules in `artifacts/hashing.py`.

## 6. Feature detection of event nesting (spec 4.7)

Codex requires per-file detection because the two generations persist user text in different
places. Never decide by `cli_version`. Per file:

1. Parse `session_meta`; note `history_mode` when present.
2. Scan line types. If at least one `event_msg` with payload type `user_message` exists, use
   Channel A and ignore user-role `response_item` messages for extraction (they duplicate
   Channel A plus injected context).
3. Otherwise, if user-role `response_item` `message` lines exist, use Channel B with the tag
   filter.
4. Otherwise the file yields zero candidates (empty session, not an error).

Cross-check (cheap, per file): when both channels exist, every Channel A text should also appear
among Channel B texts by exact hash. A mismatch raises a non-fatal `channel_mismatch` diagnostic
and keeps Channel A output. Local verification on a real store showed 95/95 Channel A texts
matched Channel B, with Channel B carrying 5 extra injected-context messages — which is exactly
why Channel A wins when present.

## 7. Deduplication considerations

Within-adapter duplicates the normalizer must expect:

1. Same file, both channels: handled by channel selection (6); never extract both.
2. `compacted` lines with `replacement_history`: re-embed earlier messages after context
   compaction. The adapter never extracts from `compacted` payloads. This also avoids the known
   file-bloat failure mode (upstream issue: session logs growing to hundreds of MB from repeated
   compaction history).
3. Forked sessions: a file with `forked_from_id` may repeat the parent's user turns. Cross-file
   exact-hash dedup with identical or near-identical line timestamps collapses these; the
   canonical copy is the earliest file.
4. Resumed sessions append to the same file; repeated `session_meta` lines do not duplicate user
   text by themselves.
5. WSL dual instances (WSL-side plus Windows-host mount) are distinct stores of distinct
   sessions in the normal case; if a user shares one `CODEX_HOME` across both, exact-hash plus
   session-ID dedup collapses the overlap.
6. Cross-source duplicates (for example, dictation pasted into Codex) are handled by the shared
   normalizer (spec 4.8), not by this adapter.

## 8. Safe discovery and snapshot behavior

Discovery (read-only, local, no network, no model):

- Resolve roots per 2.1/2.4. For each root, enumerate only the allowlist paths (2.2).
- Parse files with a streaming JSONL reader. Tolerate: an empty file, blank lines, a truncated
  final line (the live session file is append-only and may be mid-write), unknown line types,
  and unknown payload keys. A truncated final line is dropped with a per-file
  `truncated_tail` note, not an error.
- Compute per instance: file count, supported/unsupported file counts, candidate user-message
  count, candidate word and byte counts, earliest/latest included-line timestamps, detected
  `cli_version` range, storage-variant fingerprint. Return only the
  `InstanceInventorySummary`; never any text, path, or filename.
- Performance: only `session_meta` and candidate lines need full JSON parsing; other lines can
  be classified by a bounded prefix match on `"type":"..."`. Date-directory pruning (5.3) keeps
  the five-minute onboarding target on stores with thousands of files.

Snapshot:

- Plain file copy of the selected rollout files into
  `<repository>/runtime/runs/<run-id>/snapshots/codex/<instance-hash>/`, preserving the
  `sessions/YYYY/MM/DD` and `archived_sessions` relative layout, after the snapshot-path safety
  checks in the project specification (3.6).
- Copy is byte-for-byte; no locking is required for append-only JSONL. The file currently being
  appended by a live Codex session is copied as-is; the truncated-tail rule makes the copy safe.
- Snapshot manifest records source file sizes, SHA-256 hashes, and mtimes for the cleanup
  manifest and lineage. Extraction runs only against the snapshot.
- Permissions caution: upstream Codex has created session files world-readable (0644) on Unix.
  Snapshot copies are written 0600 in 0700 directories regardless of source permissions.
- Never write, rename, lock, or delete anything under the Codex root. Archived/unarchived state
  is read, never changed.

## 9. Failure behavior when evidence is insufficient

- File whose first line is not a valid `session_meta` `RolloutLine` (including pre-2025-09 bare
  format): count as `detected, unsupported schema` for that file; report the per-instance count;
  contribute no text.
- Instance where every parseable file is unsupported: instance state `detected, unsupported
  schema` with diagnostic code; not selectable.
- Unknown `history_mode` value: that file is unsupported (5.1).
- Unknown line types or payload keys inside an otherwise supported file: skip and count; do not
  fail the file.
- JSON parse error on a non-final line: the file is malformed; extract nothing from it, report a
  `malformed_file` diagnostic with counts only. Other files in the instance are unaffected.
- Root present but `sessions/` unreadable (permissions): instance state `inaccessible` with
  diagnostic code; never elevate privileges.
- The adapter never guesses: any shape not matching sections 3 and 5 yields exclusion plus a
  countable diagnostic, not a heuristic parse.

## 10. Unresolved questions

Tracked for refresh before the release-candidate freeze (spec 13.2 requires research refresh on
new storage generations):

1. `paginated` history mode end state: whether user text in paginated files ever moves out of
   `response_item` `message` into `item_completed` payloads. Current evidence says
   `response_item` remains present; Channel B covers it. Re-verify on the frozen release version.
2. Exact `kind` removal boundary: which release stopped writing `kind` on `user_message`.
   Not needed for correctness (rules do not branch on version), only for the compatibility
   matrix.
3. Compaction bridge messages in older versions (pre-`compacted` line type): whether any
   generation injected summary text as a plain user-role message without a tag wrapper. Mitigated
   by dedup and the authorship filter; needs a targeted fixture if evidence appears.
4. Full value set of string-form `source` (`"cli"`, `"vscode"`, others such as exec/MCP-driven
   sessions). Non-subagent unknown values remain eligible; revisit if an automation-driven value
   is found that should be excluded.
5. Windows-host-from-WSL enumeration: confirm DrvFS metadata behavior (mtimes, permissions) on a
   real dual setup during the WSL smoke test.
6. Whether `CODEX_HOME` is still honored identically on all three OSes in the frozen release.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/codex/<variant>/` with `fixture.json` metadata. All content is
synthetic; fake secrets are unmistakably fake. Filenames follow the real
`rollout-<date>T<time>-<uuid>.jsonl` pattern with fixed synthetic UUIDs.

| Variant | Contents | Expected result |
|---|---|---|
| `success-legacy-events` | Wrapped format; `session_meta` (with `git`, `instructions`); `event_msg` `user_message` with `kind: "plain"` plus one `kind: "user_instructions"` and one `kind: "environment_context"`; matching `response_item` messages; reasoning, function calls, token counts | Channel A; only the plain messages extracted; injected kinds excluded |
| `success-modern-legacy` | Newer meta (`base_instructions`, `history_mode: "legacy"`, string `source`); `user_message` without `kind`; user-role `response_item` with `<environment_context>` and AGENTS expansions; `turn_context`, `world_state` | Channel A; tag filter proves Channel B extras would be excluded; cross-check passes |
| `success-paginated` | `history_mode: "paginated"`; no `event_msg` `user_message`; user/assistant/developer `response_item` messages, one with `input_image` part | Channel B; user `input_text` only; developer excluded |
| `success-archived` | Same as `success-modern-legacy` but under `archived_sessions/` | Inventoried and extracted identically |
| `subagent-excluded` | Valid file whose `session_meta.source` is `{"subagent": {...}}` and `parent_thread_id` set | Whole file excluded from candidates; counted as ineligible session |
| `empty` | (a) zero-byte file; (b) file with only `session_meta`; (c) empty date directories | Zero candidates; no errors |
| `malformed` | (a) truncated final line; (b) invalid JSON on a middle line; (c) non-UTF-8 bytes | (a) tail dropped, rest extracted; (b)+(c) `malformed_file`, zero candidates from that file |
| `unsupported` | (a) pre-2025-09 bare-item file with `record_type` keys; (b) first line valid JSON but not `session_meta`; (c) unknown `history_mode` | `detected, unsupported schema` per file; instance stays usable |
| `migration` | Parent file plus fork file with `forked_from_id` repeating two user turns; plus a `compacted` line with `replacement_history` | Fork duplicates collapse to earliest copy; nothing extracted from `compacted` |
| `dual-root-wsl` | Two synthetic roots (WSL-style and `/mnt/c/Users/<fake>/.codex`-style) fed through path resolution | Two instances, separate labels, no merge |

Platform test axes (spec 13.2): every variant parses identically on macOS, Linux, native Windows,
and WSL runners; path-resolution tests cover `CODEX_HOME` override, missing root, unreadable
`sessions/`, and Windows profile-style paths. A private smoke test against at least one real
installation per claimed platform is required before stable release; its outputs are never
committed.

Deterministic assertions per success variant: candidate counts, word counts, per-utterance text
hashes, timestamps, channel decision, and diagnostic codes are golden-file asserted.

## 12. Reproducible read-only inspection commands

Safe on a real installation (structure only, no content printed):

```bash
# Layout and generation survey
find "${CODEX_HOME:-$HOME/.codex}/sessions" -maxdepth 3 -type d | head
ls "${CODEX_HOME:-$HOME/.codex}/archived_sessions" 2>/dev/null | head -3

# Line-type census of one file (no payload content)
python3 -c 'import json,sys,collections
c=collections.Counter()
for l in open(sys.argv[1], errors="replace"):
    try: o=json.loads(l)
    except Exception: c["<bad>"]+=1; continue
    p=o.get("payload") or {}
    c[str(o.get("type"))+":"+str(p.get("type") if isinstance(p,dict) else "")]+=1
print(c.most_common())' <one rollout file>

# session_meta key set only
python3 -c 'import json,sys
o=json.loads(open(sys.argv[1], errors="replace").readline())
print(o.get("type"), sorted((o.get("payload") or {}).keys()))' <one rollout file>
```

Never run `cat`, `jq .`, or any command that prints payload text from a real store.
