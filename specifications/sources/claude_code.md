# Claude Code source specification

Adapter ID: `claude_code`
Status: reviewed source specification (spec section 4.2 research gate)
Research log: `temp/findings/claude-code-source-research.md` (evidence IDs E1–E10 cited below)
Tested application versions: Claude Code 2.1.201–2.1.226, macOS (direct structural observation)
Access date for all cited evidence: 2026-08-08

Claude Code stores each CLI conversation as an append-only JSONL transcript. This document
defines where those transcripts live per platform, which records contain user-authored text,
which files must never be opened, and how the adapter discovers, snapshots, extracts, and fails.

Anthropic states the transcript format is internal and may change in any release (E2). The
adapter therefore feature-detects record shapes and fails closed to "detected, unsupported
schema" instead of guessing.

## 1. Platform status and storage locations

Never infer one platform's behavior from another. Each row below carries its own evidence.

| Platform | Status | Transcript root | Evidence |
| --- | --- | --- | --- |
| macOS | Supported, tested | `~/.claude/projects/` | E1, E6 (strong) |
| Linux (native) | Supported, untested locally | `~/.claude/projects/` | E1, E4 (moderate) |
| Windows (native) | Supported, needs fixture verification | `%USERPROFILE%\.claude\projects\` | E3 (moderate) |
| WSL | Supported for the WSL-side store only | `~/.claude/projects/` inside the WSL home | E4 (moderate) |

Details:

- The storage root moves when `CLAUDE_CONFIG_DIR` is set. Discovery must check
  `$CLAUDE_CONFIG_DIR/projects/` first, then the default root (E2, strong).
- Project directory names encode the session's working directory: every non-alphanumeric
  character is replaced with `-`. Example (synthetic): cwd `/home/user/repos/myapp` becomes
  `-home-user-repos-myapp` (E1, strong). On Windows the same rule applies to the native path,
  giving names like `C--Users-alice-repos-myapp`; the exact drive-letter form is unverified and
  must be confirmed against a Windows fixture before release (E3, moderate).
- Session files: `projects/<encoded-cwd>/<session-uuid>.jsonl`, one JSON object per line. The
  file name is the session ID (E1, E8, strong).
- Per-session subdirectory `projects/<encoded-cwd>/<session-uuid>/` may exist and contains
  `subagents/` transcripts and `tool-results/` blobs (E6, strong; current generation). The
  adapter never reads it (section 5).
- WSL: Claude Code installed inside WSL is a Linux install with its own `~/.claude`. A native
  Windows install's store is a different dataset, reachable from WSL only via
  `/mnt/<drive>/Users/<user>/.claude/`. V1 discovers only the WSL-side store when running in
  WSL. The mounted Windows-host store is plain files and technically readable, but it is a
  separate untested variant; V1 does not auto-discover it and instead reports a hint diagnostic
  when the mount path exists, directing the user to run the audit from native Windows for that
  data (E4, moderate; spec section 1.3).
- Retention: transcripts older than `cleanupPeriodDays` (default 30 days) are deleted by Claude
  Code. Discovery must present the observed date range honestly; short history is normal, not an
  error (E2, strong).
- Transcript writing can be suppressed (`CLAUDE_CODE_SKIP_PROMPT_HISTORY`,
  `--no-session-persistence`). An installed Claude Code with no `projects/` content is
  "found, empty", not "not found" (E2, strong).
- Desktop app, Claude Code on the web, and the VS Code extension keep separate histories; this
  adapter covers only the CLI transcript store (E10, weak-to-moderate).

## 2. Files that must never be opened

The adapter opens only `projects/<encoded-cwd>/<session-uuid>.jsonl` files at depth 2 under the
storage root. Everything else in the root is denylisted. Explicitly forbidden (E5, strong):

- `.credentials.json` (Linux/Windows OAuth tokens; may transiently exist on macOS).
- The macOS Keychain item `Claude Code-credentials`. Never invoke `security(1)`.
- `settings.json`, `settings.local.json`, and any `managed-settings.json`.
- `~/.claude.json` (config plus `lastSessionFirstPrompt` user text per project).
- `history.jsonl` (global prompt history with paste bodies; duplicates transcript text).
- `projects/<encoded-cwd>/<session-uuid>/` subdirectories (`subagents/`, `tool-results/`).
- `shell-snapshots/`, `file-history/`, `paste-cache/`, `uploads/`, `backups/`, `plans/`,
  `debug/`, `telemetry/`, `stats-cache.json`, `sessions/`, `session-env/`, `tasks/`,
  `plugins/`, `skills/`, `CLAUDE.md`, and any other root entry.

Rationale: the denylisted files contain credentials, config, or duplicate user text without
session structure. Reading them adds risk and no extraction value. The single-glob allowlist is
enforced in code; a denylist check is defense in depth.

## 3. JSONL record schema

### 3.1 Current generation (observed at 2.1.201–2.1.226; E6, strong, reverse-engineering)

Conversation records share an envelope: `uuid`, `parentUuid` (string or null), `timestamp`
(ISO 8601, UTC with `Z`), `sessionId`, `cwd`, `version` (Claude Code semver), `gitBranch`,
`userType` (`"external"` observed), `entrypoint`, `isSidechain` (bool), optional `isMeta`
(bool), optional `slug`, optional `session_id` (snake_case duplicate), optional
`logicalParentUuid`.

Top-level `type` values observed:

- `user` — carries `message` with `role: "user"`. `message.content` is either:
  - a string: a typed prompt, a slash-command wrapper, or local-command output; or
  - an array of blocks: `text`, `image`, or `tool_result` (tool results are delivered as user
    records; those records also carry top-level `toolUseResult` and `sourceToolAssistantUUID`).
  Newer prompt records add `origin` (object with key `kind`) and `promptSource`. Observed:
  `origin.kind: "human"` with `promptSource` `typed`/`queued`/`suggestion_accepted`, and
  `origin.kind: "task-notification"` with `promptSource: "system"`. Older records within the
  same generation lack `origin` entirely.
- `assistant` — `message` with `role: "assistant"`, content block types `text`, `thinking`,
  `tool_use`, `fallback`; `usage` token accounting.
- `system` — carries `subtype`; observed: `turn_duration`, `away_summary`, `local_command`,
  `stop_hook_summary`, `compact_boundary`, `bridge_status`, `scheduled_task_fire`,
  `informational`, `model_refusal_fallback`. `compact_boundary` records carry `compactMetadata`
  (`trigger`, `preTokens`, `postTokens`, `cumulativeDroppedTokens`, ...) and a string summary in
  `content`.
- `attachment` — injected context deltas (`task_reminder`, `skill_listing`,
  `agent_listing_delta`, `deferred_tools_delta`, `mcp_instructions_delta`).
- Bookkeeping types: `ai-title` (generated title), `last-prompt` (keys `type`, `lastPrompt`,
  `leafUuid`, `sessionId`; duplicates the latest prompt text), `queue-operation` (may carry
  queued prompt text in `content`), `mode`, `permission-mode`, `file-history-snapshot`,
  `file-history-delta`, `agent-name`, `pr-link`, `relocated`, `worktree-state`,
  `bridge-session`, `frame-link`.

String-content markers observed inside `user` records:

- Slash-command invocations: content starts with `<command-name>` and uses `<command-message>`
  and `<command-args>` tags.
- Local command output: content wrapped in `<local-command-stdout>...</local-command-stdout>`.
- Injected reminders: `<system-reminder>...</system-reminder>` blocks may be appended to or
  delivered alongside genuine prompts (E6, E7).
- Compaction: a `user` record with `isCompactSummary: true` (and
  `isVisibleInTranscriptOnly: true`) holds the generated summary, not user text.

### 3.2 Legacy generation (secondary sources; E7, moderate)

Files written by earlier versions (2025 through early 2026) may additionally contain:

- `summary` records: `{"type": "summary", "summary": "...", "leafUuid": "..."}`, often at the
  file head, sometimes referencing a leaf in another file.
- Inline sidechains: subagent conversations in the same file with `isSidechain: true` on `user`
  and `assistant` records (current versions move these to `subagents/`).
- Bash passthrough tags `<bash-input>`, `<bash-stdout>`, `<bash-stderr>` in user records.
- Fewer bookkeeping types; absence of `origin`/`promptSource`.

Version boundaries for these migrations are not published. The adapter feature-detects per
record and never branches on the `version` field alone.

### 3.3 Subagent transcripts

`projects/<encoded-cwd>/<session-uuid>/subagents/agent-<hex>.jsonl` plus
`agent-<hex>.meta.json` (`agentType`, `description`, `toolUseId`, `spawnDepth`), and
`subagents/workflows/wf_<id>/journal.jsonl`. Their `user` records are prompts written by the
parent agent, not by the human (E6, E9). Excluded entirely: the directory is never read.

## 4. Inclusion and exclusion rules

### 4.1 Inclusion — a record contributes candidate user-authored text only if all hold

1. Top-level `type == "user"` and `message.role == "user"`.
2. `isMeta` is absent or `false`.
3. `isSidechain` is absent or `false`.
4. `isCompactSummary` is absent or `false`.
5. `userType` is absent or `"external"`.
6. If `origin` is present, `origin.kind == "human"`. If `promptSource` is present, it is not
   `"system"`. Absence of both fields does not exclude (older records lack them).
7. Text is taken only from:
   - a string `message.content`, after wrapper handling in 4.2; or
   - `text` blocks inside an array `message.content`, after wrapper handling in 4.2.

Extraction metadata per kept utterance: `uuid` (stable local utterance ID basis), `sessionId`,
`timestamp`, `version`, modality `written`, text status `verbatim`, authorship basis
`explicit_user_role` (plus `origin_human` when field 6 was present).

Modality note: dictation into a terminal is invisible in the transcript. Per spec section 5.5,
coding-agent text not positively matched to a raw voice source is `written`.

### 4.2 Exclusion and wrapper handling

Excluded record kinds (never contribute text):

- `assistant`, `system`, `attachment`, `summary`, `ai-title`, `last-prompt`,
  `queue-operation`, `mode`, `permission-mode`, `file-history-*`, `agent-name`, `pr-link`,
  `relocated`, `worktree-state`, `bridge-session`, `frame-link`, and any unknown type.
- `user` records failing any condition in 4.1, including tool_result carriers (top-level
  `toolUseResult` present, or any `tool_result` block in content).
- `image` and `tool_result` blocks inside kept records.

Wrapper handling inside otherwise-kept string or text-block content:

- Content starting with `<command-name>`: exclude the record. `<command-args>` may contain
  short user-typed arguments, but they are command syntax, not prose; V1 drops them
  (precision over recall).
- Content containing `<local-command-stdout>`, `<bash-input>`, `<bash-stdout>`, or
  `<bash-stderr>`: exclude the record.
- `<system-reminder>...</system-reminder>` spans: strip the spans; keep the remainder if
  non-empty. Unbalanced reminder tags: exclude the record and count a structural warning.
- Any other `<tag>`-prefixed content matching the known injected-wrapper set: exclude. Unknown
  leading tags: keep the text but flag `content_flags: ["unknown_wrapper"]` so normalization can
  quarantine it.

Paste artifacts: pasted text is user-transferred, not user-authored. The record-level
`imagePasteIds` field marks image pastes (blocks already excluded). Long pasted text is not
structurally distinguishable in the transcript; the shared normalization layer removes
copied/pasted/code-like material (spec section 4.5). The adapter passes `content_flags:
["possible_paste"]` when a single prompt exceeds a configured length threshold.

Credential safety: the adapter never opens denylisted files (section 2). Text that itself
contains secrets is handled downstream by the privacy pipeline; the adapter adds no
credential parsing of its own.

## 5. Sessions, timestamps, and deduplication

- Session identity: the transcript file name (a UUID) and the per-record `sessionId`. Records
  whose `sessionId` differs from the file name are possible (interleaved resumes, hand-copied
  files); the adapter keys utterances by `(file_session_id, record_sessionId, uuid)` and
  reports a mismatch diagnostic without failing the file.
- Timestamps: per-record `timestamp`, ISO 8601 UTC. Session range = min/max over kept records.
  Records without a parsable timestamp are kept for counting but excluded from period filters
  only if the period is bounded; they are reported as `undated` (spec: `None`, never zero).
- Continued sessions (`--continue`, `--resume`): append to the same file. No duplication.
- Compacted sessions: compaction inserts `compact_boundary` and summary records in the same
  file; original user records remain. No duplication, summaries excluded by rule.
- Forked sessions (`/branch`, `--fork-session`): the transcript is copied into a new session ID,
  duplicating historical records with identical `uuid` values (E8, strong). Dedup rule: within
  one discovery pass over all Claude Code instances, utterances with the same `uuid` and equal
  normalized text collapse to one canonical utterance; the earliest file timestamp wins.
- Relocated sessions (`/cd`, `relocated` records): the file moves between project directories;
  `uuid` dedup also covers a stale copy left behind by a crash.
- Cross-source dedup (same sentence dictated in Wispr Flow then pasted here) is the shared
  normalizer's job (spec section 4.8); this adapter only supplies text hashes and timestamps.

## 6. Discovery, snapshot, extraction, verification

### 6.1 discover()

1. Resolve root: `$CLAUDE_CONFIG_DIR` if set, else the platform default (section 1). WSL: the
   WSL home only.
2. If `projects/` is missing or empty: state `found` with zero instances when the root exists
   (installed but no transcripts), else `not_found`.
3. Enumerate `projects/*/`. Each project directory is one source instance. Return only the
   ephemeral opaque label, candidate counts, and date range (spec section 2.4); never the
   encoded directory name.
4. For each `*.jsonl` at depth 2: stream-parse lines, apply section 4 rules, and accumulate
   candidate message, word, and byte counts plus min/max timestamps. Tolerate malformed lines
   (section 7). Never print, log, or return text.
5. Storage variant fingerprint: set of observed top-level `type` values and presence of
   `origin`, `summary`, inline `isSidechain: true`. Report the matched variant
   (`v2-current`, `v1-legacy`, or `mixed`) as the schema fingerprint.

### 6.2 snapshot()

Transcripts are plain append-only files; no SQLite, no WAL (E1, E6). Snapshot = byte copy of
each selected `*.jsonl` into `<repository>/temp/runtime/<run-id>/snapshots/claude_code/`
following the spec 3.6 preflight (path resolution, symlink refusal, git-ignore check,
cloud-sync refusal). Because Claude Code may append during the copy, the snapshot reader must
drop a truncated final line silently. Snapshot manifest records source file size, mtime, and
SHA-256 at copy time. Live files are opened read-only and never locked or modified.

### 6.3 extract()

Runs only against the snapshot. Emits `NormalizedUtterance` records per spec section 4.4 with
modality `written`, text status `verbatim`, and a source-path hash of the canonical original
path (never the path itself).

### 6.4 verify()

Adapter-specific deterministic checks: every utterance maps to a snapshot line whose parsed
record passes section 4.1; no utterance text contains a known wrapper tag; no denylisted file
was opened (audit of opened-path log); counts are internally consistent; dedup produced no
orphan canonical references.

## 7. Failure behavior

- Malformed line (JSON parse failure, non-object, missing `type`): skip, increment a
  per-file malformed counter. If malformed lines exceed 10% of non-empty lines in a file, mark
  that file `unsupported_schema` and exclude it from extraction.
- File where zero records match any known conversation shape: `detected, unsupported schema`
  diagnostic for that file; discovery still reports the instance with the affected file counted
  as unsupported.
- Instance where every file is unsupported: instance state `unsupported`, no candidate counts,
  stable diagnostic code, no guessing (spec section 4.2).
- Unknown future record types: ignored individually (forward-compatible), but a `user` record
  whose `message` shape is unrecognized (no `role`, or content neither string nor array) makes
  the record excluded and counted as `unrecognized_user_record`; if such records dominate a
  file, the file falls to `unsupported_schema`.
- Permission errors, unreadable root: instance state `inaccessible` with diagnostic code;
  never retried with elevated privileges.
- The adapter never writes to, locks, truncates, or deletes anything under the storage root.

## 8. Unresolved questions and required behavior when evidence is insufficient

1. Windows encoded-directory form (drive letter, UNC paths): unverified. Behavior: discovery
   enumerates whatever directories exist; no path reconstruction depends on the encoding. A
   Windows fixture check is required before the Windows platform gate passes.
2. Legacy-generation version boundaries (`summary` records, inline sidechains, `subagents/`
   split): unknown. Behavior: per-record feature detection; no version gating.
3. `queue-operation` records may hold typed text that never became a `user` record (abandoned
   queue entries). Behavior: excluded in V1; documented as a small recall loss.
4. `origin`/`promptSource` semantics beyond observed values: values outside the observed set
   exclude the record (fail closed) and increment an `unknown_origin_kind` counter.
5. Desktop-app or VS Code artifacts under `projects/`: unconfirmed. Behavior: covered by the
   malformed/unsupported thresholds in section 7.
6. Mounted Windows-host store in WSL: readable in principle, untested. Behavior: not
   discovered; hint diagnostic only (section 1).

## 9. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/claude_code/<variant>/` with a `fixture.json` metadata file.
All content is synthetic; secret-looking values are unmistakably fake.

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-current` | v2.1-style session: typed prompts (string and text-block, with and without `origin`), tool_result carriers, assistant/system/attachment records, `<command-name>` wrapper, `<local-command-stdout>`, embedded `<system-reminder>` span, `isMeta` record, compact boundary + `isCompactSummary` record, `ai-title`, `last-prompt`, plus an unread `subagents/` subtree | exact kept-utterance set, wrapper stripping, subagents never opened |
| `success-legacy` | v1-style session: head `summary` record, inline `isSidechain: true` block, string prompts, `<bash-input>`/`<bash-stdout>` records, no `origin` | legacy records classified, sidechain and summary excluded |
| `empty` | project dir with a zero-length `.jsonl`; second project dir with no files; root with `projects/` absent | found-empty vs not-found states |
| `malformed` | valid lines mixed with truncated final line, one mid-file garbage line, one non-object line; second file over the 10% threshold | truncated-tail drop, per-file thresholding, no crash |
| `unsupported` | records with unknown `type` only; `user` records with unrecognized `message` shape | `detected, unsupported schema`, zero extracted text |
| `migration-fork` | two session files where one is a fork copy sharing historical `uuid`s | uuid dedup keeps one canonical utterance |
| `migration-mixed` | one file containing both legacy and current record shapes | mixed fingerprint, both generations extracted |
| `windows-encoding` | encoded-cwd directory named like `C--Users-tester-repos-app` | discovery is encoding-agnostic |
| `config-dir-override` | populated root reachable only via `CLAUDE_CONFIG_DIR` | override honored, default root ignored |
| `denylist` | root containing fake `.credentials.json` (`sk-FAKEFAKEFAKE0000`), `settings.json`, `history.jsonl` | opened-path audit proves none were read |

Platform matrix: the full pytest suite runs on macOS, Linux, WSL, and native Windows CI or
manual gates. Path handling, encoded-name enumeration, and permission-error fixtures run per
platform; Windows release requires the section 8.1 encoding check.

## 10. Reproducible read-only inspection commands

Structure-only commands used for this research; safe to rerun. Placeholder paths only. They
print field names, types, and counts — never values.

```bash
ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Directory layout (names only)
ls "$ROOT"
ls "$ROOT/projects" | wc -l

# Top-level record types across session files
find "$ROOT/projects" -maxdepth 2 -name '*.jsonl' | while read -r f; do
  jq -r '.type // "NOTYPE"' "$f" 2>/dev/null
done | sort | uniq -c | sort -rn

# Key sets of user records (field names only)
find "$ROOT/projects" -maxdepth 2 -name '*.jsonl' | while read -r f; do
  jq -c 'select(.type=="user") | keys_unsorted' "$f" 2>/dev/null
done | sort | uniq -c | sort -rn | head

# Content form vs authorship flags (booleans and enums only)
find "$ROOT/projects" -maxdepth 2 -name '*.jsonl' | while read -r f; do
  jq -r 'select(.type=="user")
    | [(.message.content|type), (.isMeta//false|tostring), (.isSidechain|tostring),
       (.origin.kind? // "none"), (.promptSource // "none")] | join(" ")' "$f" 2>/dev/null
done | sort | uniq -c

# Wrapper-tag marker counts (no content printed)
find "$ROOT/projects" -maxdepth 2 -name '*.jsonl' | while read -r f; do
  jq -r 'select(.type=="user" and (.message.content|type=="string")) | .message.content
    | [(startswith("<command-name>")|tostring), (contains("<local-command-stdout>")|tostring)]
    | join(" ")' "$f" 2>/dev/null
done | sort | uniq -c
```

Never run `jq .` or any command that prints record values against real transcripts.
