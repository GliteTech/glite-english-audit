# Source specification: OpenCode (`opencode`)

Status: reviewed source specification for the `opencode` adapter (spec section 4.2 research gate).
Adapter ID: `opencode`. Stability: stable (after release gates pass).
Research basis: primary vendor source code (shallow clone of the vendor repository, dev branch
commit `38e10eb1` of 2026-08-08, plus released tags `v0.0.1`–`v1.2.0`), vendor documentation,
release notes, and maintainer issue threads. No local installation was inspected; nothing in this
document comes from a real user store. The private research log with per-claim evidence lives in
`temp/findings/opencode-source-research.md` during development.

Tested application generation at research time: OpenCode v1.18.x (2026-08, TypeScript, repository
`anomalyco/opencode`, formerly `sst/opencode`). Earliest generation this adapter supports:
TypeScript OpenCode v0.0.x (2025-04) JSON trees. The unrelated Go application that previously
used the same name (`opencode-ai/opencode`, archived 2025-06 at v0.0.55, continued as
`charmbracelet/crush`) is a different product with per-project SQLite storage; it is out of scope
and is never discovered by this adapter (section 2.4).

## 1. Platform status summary

| Platform | Status | Storage root |
|---|---|---|
| macOS | Supported | `${XDG_DATA_HOME:-~/.local/share}/opencode/` |
| Linux (native) | Supported | `${XDG_DATA_HOME:-~/.local/share}/opencode/` |
| Windows (native) | Supported, needs fixture verification | `%USERPROFILE%\.local\share\opencode\` (or `%XDG_DATA_HOME%\opencode\`) |
| WSL | Supported for the WSL-side store only | `~/.local/share/opencode/` inside the WSL home |

OpenCode resolves all of its paths through the `xdg-basedir` npm package, which has no Windows
special case: on every OS the data root is `$XDG_DATA_HOME/opencode` when the variable is set,
otherwise `<home>/.local/share/opencode`. Vendor docs confirm the same
`%USERPROFILE%\.local\share\opencode` layout on Windows. `%APPDATA%` is not used despite one
informal vendor post saying so; the released code reads only the xdg path.

The current storage generation is a WAL-mode SQLite database, so the section 4.6 database rules
apply. Two legacy JSON tree generations may coexist with the database on the same machine and
must be inventoried and deduplicated against it (sections 3, 6).

## 2. Storage locations

### 2.1 Root resolution

1. `$XDG_DATA_HOME/opencode` when `XDG_DATA_HOME` is set and non-empty.
2. Otherwise `<home>/.local/share/opencode` (`%USERPROFILE%\.local\share\opencode` on Windows).

There is no supported whole-data-dir override. The `OPENCODE_DB` environment variable can move
or rename only the database file (absolute path, or a filename joined onto the data root);
discovery honors it when set. `OPENCODE_TEST_HOME` affects only test isolation and is ignored.

### 2.2 Files the adapter may open (allowlist)

```text
<root>/opencode.db                         current SQLite database (prod/latest/beta channels)
<root>/opencode-<channel>.db               channel-suffixed database variants (see 3.1)
<root>/opencode*.db-wal, *.db-shm          WAL side files, via the snapshot procedure only
<root>/storage/migration                   integer migration marker for the JSON generations
<root>/storage/session/<projectID>/ses_*.json      Gen J2 session info
<root>/storage/message/<sessionID>/msg_*.json      Gen J2 messages
<root>/storage/part/<messageID>/prt_*.json         Gen J2 parts
<root>/project/<slug>/storage/session/info/ses_*.json               Gen J1 session info
<root>/project/<slug>/storage/session/message/<sessionID>/msg_*.json Gen J1 messages
<root>/project/<slug>/storage/session/part/<sessionID>/<messageID>/prt_*.json Gen J1 parts
```

Nothing else under the root is ever opened.

### 2.3 Files and tables that must NEVER be opened

Files, in addition to the allowlist above:

- `<root>/auth.json` — provider API keys and OAuth tokens.
- `<root>/mcp-auth.json` — MCP OAuth tokens.
- `<root>/opencode.json` / `opencode.jsonc` (legacy config location at the data root) and the
  config directory `${XDG_CONFIG_HOME:-~/.config}/opencode/` — config may embed keys.
- `<root>/storage/session_share/*.json` — share secrets.
- `<root>/storage/todo/`, `<root>/storage/permission/`, `<root>/storage/session_diff/`,
  `<root>/storage/project/` — no user-authored text; project files contain private worktree paths.
- `<root>/project/<slug>/app.json` (Gen J1 per-project state) and any other file under a project
  directory besides the three allowlisted globs.
- `<root>/snapshot/`, `<root>/log/`, `<root>/repos/`, `<root>/broken/`, the cache root
  (`~/.cache/opencode/`), the state root (`~/.local/state/opencode/`), and any unknown entry.
  Unknown names are ignored silently and never returned to the agent.

SQLite tables inside an otherwise-allowlisted database. The database itself contains credentials,
so table-level discipline is mandatory:

- Never read: `account`, `account_state`, `control_account` (OAuth access/refresh tokens),
  `credential` (stored credential values), `session_share` (share secrets), `event` and
  `event_sequence` (event-sourced payloads duplicating message content), `workspace`,
  `project` and `project_directory` (private worktree paths and names), `permission`, `todo`,
  `session_input`, `session_message`, `session_context_epoch`, and any unknown table.
- Read only: `session` (specific columns, see 3.2), `message`, `part`.

The snapshot procedure must strip the never-read tables before extraction (section 8).

### 2.4 Platform specifics and out-of-scope stores

- macOS and native Linux: identical layout; no sandbox/App Store container variant exists.
- Native Windows: identical layout under `%USERPROFILE%\.local\share\opencode`. Untested against
  a real Windows install; the Windows release gate requires a fixture-verified smoke test.
- WSL: an OpenCode installed inside WSL writes to the WSL home. A Windows-host install visible at
  `/mnt/<drive>/Users/<user>/.local/share/opencode` is a separate dataset containing a live WAL
  SQLite database; a consistent snapshot through DrvFS cannot be guaranteed while the host app
  runs. Per spec 1.3 the adapter does not auto-discover the mounted store; when the mount path
  exists it reports a hint diagnostic directing the user to run the audit from native Windows.
- Go-era OpenCode (`opencode-ai/opencode`): stored a SQLite database inside a per-project
  `.opencode/` data directory configured in `~/.opencode.json`. Per-project directories cannot be
  enumerated from a single root without walking the whole disk, which discovery never does. Out
  of scope for V1; not discovered, not counted, documented here for completeness.
- The OpenCode desktop app and IDE/ACP integrations drive the same local server and data root in
  the default setup; their sessions appear in the same database. No separate variant is claimed
  and none was tested.

## 3. Storage generations and record schemas

Timeline established from released tags (dates are tag commit dates):

| Generation | Versions | Dates | Layout |
|---|---|---|---|
| J1 (per-project JSON tree) | v0.0.x – v0.5.x | 2025-04 – 2025-08 | `<root>/project/<slug>/storage/...` |
| J2 (flat global JSON tree) | v0.6.0 – v1.1.x | 2025-09-01 – 2026-02 | `<root>/storage/...` |
| S (SQLite) | v1.2.0 – current | 2026-02-14 – present | `<root>/opencode*.db` |

Upgrades ran in-place migrations (J1→J2 at v0.6.0; J2→S at v1.2.0). The v1.2.0 JSON-to-SQLite
migration copied rows into the database and deliberately did not delete the JSON tree, and several
released migration bugs skipped or partially completed the copy. Therefore all three generations
may coexist under one root, with overlapping or complementary content. The adapter always
inventories every generation present and deduplicates by record ID (section 7).

### 3.1 Generation S — SQLite database (v1.2.0+)

- File: `<root>/opencode.db` for the `prod`, `latest`, and `beta` install channels;
  `<root>/opencode-<channel>.db` for other channels. Releases around v1.15.12 briefly named the
  prod database `opencode-prod.db` before current code mapped prod back to `opencode.db`, so both
  names can exist with overlapping content. Discovery globs `opencode*.db` at the root and treats
  each database as a store to dedup, honoring `OPENCODE_DB` when set.
- Mode: `journal_mode = WAL`; `-wal`/`-shm` side files are expected next to a recently used
  database. Foreign keys on; no encryption; no compression.
- Schema management: Drizzle ORM with dated migrations applied at startup. There is no schema
  version column; fingerprint by probing `sqlite_master` for required tables/columns.

Tables the adapter reads:

`session` — one row per conversation. Columns used: `id` (TEXT, `ses_*`), `project_id`,
`parent_id` (TEXT nullable; non-null marks a subagent/subtask child session), `time_created`,
`time_updated` (INTEGER, Unix epoch milliseconds), `time_archived` (nullable; archived sessions
remain eligible). Columns present but never returned: `directory`, `path`, `title` (generated),
`slug`, `version` (writing app version, reported as app_version range), `share_url`, `metadata`,
token/cost columns, `revert`, `permission`, `agent`, `model`, `workspace_id`. Later releases
added columns (`workspace_id` 2026-02, `path` 2026-04, `metadata` 2026-05, usage columns
2026-05); probe before selecting.

`message` — `id` (TEXT, `msg_*`), `session_id`, `time_created`, `time_updated` (ms epoch),
`data` (TEXT JSON). `data` is the v1 message object minus `id`/`sessionID`:

- user message: `{"role": "user", "time": {"created": <ms>}, "agent": ..., "model": {...},
  "summary"?: {...}, "system"?: ..., "tools"?: {...}, "format"?: {...}}`. Rows migrated from
  JSON generations may lack `agent`/`model`; tolerate absence.
- assistant message: `{"role": "assistant", ...}` with token, cost, path, and error fields.

`part` — `id` (TEXT, `prt_*`), `message_id`, `session_id`, `time_created`, `time_updated`,
`data` (TEXT JSON). `data` is the v1 part object minus `id`/`sessionID`/`messageID`,
discriminated by `type`:

- `text`: `{"type": "text", "text": "...", "synthetic"?: bool, "ignored"?: bool, "time"?: {...},
  "metadata"?: {...}}`. The only part type that can carry user-authored text.
- Excluded types: `reasoning`, `tool`, `file`, `subtask`, `agent`, `step-start`, `step-finish`,
  `snapshot`, `patch`, `retry`, `compaction`, and any unknown type.

Newer releases (2026-06+) also write an event-sourced v2 layer (`session_message`,
`session_input`, `event` tables) whose user rows duplicate the typed prompt text. The projector
keeps upserting the v1 `message`/`part` tables from those events, so the adapter reads only
`message`/`part` and never the v2 tables (dedup hazard plus `session_input.prompt` is a
verbatim duplicate). If a future release stops projecting into `message`/`part`, that database
fingerprint is unsupported until research is refreshed (section 10.1).

### 3.2 Generation J2 — flat global JSON tree (v0.6.0 – v1.1.x)

Under `<root>/storage/`:

- `session/<projectID>/ses_<id>.json` — session info object: `id`, `projectID`, optional
  `parentID`, `directory`, `title`, `version`, `time: {created, updated}` (ms epoch), optional
  `share`, `revert`, `summary`. Field growth across versions; tolerate unknown keys.
- `message/<sessionID>/msg_<id>.json` — v1 message object including `id`, `sessionID`, `role`,
  `time`, and role-specific fields as in 3.1.
- `part/<messageID>/prt_<id>.json` — v1 part object including `id`, `sessionID`, `messageID`,
  `type`, and type-specific fields as in 3.1.
- `migration` — plain integer count of applied JSON-layer migrations (2 = current J2 layout).

### 3.3 Generation J1 — per-project JSON tree (v0.0.x – v0.5.x)

Under `<root>/project/<slug>/storage/` where `<slug>` is the project worktree path with every
character outside `[A-Za-z0-9_]` replaced by `-`, or the literal `global` for sessions started
outside a Git worktree:

- `session/info/ses_<id>.json` — session info (as 3.2, without `projectID`).
- `session/message/<sessionID>/msg_<id>.json` — message. Three sub-shapes exist:
  - J1a (earliest): "v1 message" objects carrying a `metadata` object and inline content;
  - J1b: "v2 message" objects with an inline `parts` array on the message JSON;
  - J1c (late J1, after in-place migrations): message info only, with parts split out to
    `session/part/<sessionID>/<messageID>/prt_<id>.json`.
- The slug is derived from a private path. It is used only for enumeration and is hashed, never
  returned.

J1a inline content predates the part model. For J1a and J1b the adapter extracts from the inline
`parts` array (same part discrimination as 3.1); for J1c from the part files. A message whose
shape matches none of the three sub-shapes is counted `unsupported_schema` for that file.

### 3.4 Fingerprints

Per store: `sqlite` fingerprint = set of required tables/columns found by probing; `json-j2` =
presence of `storage/session/<projectID>/` two-level layout; `json-j1` = presence of
`project/<slug>/storage/session/info/`. A root may carry up to three fingerprints; each is
reported, and the instance schema fingerprint is the ordered set.

## 4. Provenance of text fields

- User-authored: `text` parts of `role == "user"` messages where `synthetic` is absent or false
  and `ignored` is absent or false. OpenCode persists the composer text verbatim; it does not
  rewrite, spellcheck, or enhance user input. Text status: `verbatim`. Modality: `written`
  (OpenCode has no built-in ASR path; dictated text arrives via external tools and is handled by
  cross-source deduplication).
- Injected, not user-authored (all appear inside user-role messages): parts with
  `synthetic: true`. Vendor code marks with this flag: file/MCP-resource attachment expansions,
  "Read tool was called" stubs, compaction continuation prompts, task-summary prompts, shell
  passthrough stubs, and plugin/tool-generated continuations. AGENTS.md content, skills, and
  environment context are assembled into the ephemeral system-prompt array at request time and
  are not persisted as message parts.
- Generated: assistant messages, `reasoning` parts, tool state, `compaction` parts, session
  `title`, `summary` fields.
- Known hazards without a structural marker (see 10 for required behavior):
  1. Slash-command template expansions are persisted as ordinary non-synthetic text parts of a
     user message; the template body was authored in a command file (possibly by the vendor, for
     example `/init`), not typed in the session.
  2. The plugin `chat.message` hook can append or mutate parts without setting `synthetic`.
  3. `opencode import <file|share-url>` inserts a foreign shared session into the same tables
     with no marker distinguishing it from locally produced sessions.

## 5. Inclusion and exclusion rules

### 5.1 Session eligibility (whole session)

Exclude every message of a session when any holds:

1. `parent_id` / `parentID` is non-null — subagent/subtask sessions; their "user" messages are
   composed by the orchestrating model.
2. The session row/info fails to parse against the section 3 shape for its generation
   (unsupported, counted).

`time_archived` set does not exclude a session. Archived sessions are ordinary history.

### 5.2 Candidate user text (per part)

Include a text candidate only when all hold:

1. Message `role == "user"` (from `data.role` in SQLite, `role` in JSON).
2. Part `type == "text"`.
3. `synthetic` is absent or `false`.
4. `ignored` is absent or `false`.
5. Text is non-empty after trimming.

Multiple qualifying text parts of one message are separate candidates joined by part order
(ascending part ID) into one utterance per message; part IDs are recorded for dedup.

Always excluded: assistant messages and every part inside them; all non-`text` part types;
session/message/part metadata fields (`title`, `summary`, `system`, `instructions`); every
denylisted file and table in 2.3.

Credential hygiene: the adapter never opens `auth.json`, `mcp-auth.json`, config files, or
credential-bearing tables (2.3, 8). Secrets typed into included user messages are handled by the
downstream normalization and privacy stages.

### 5.3 Sessions, timestamps, IDs

- Session ID: `ses_*` string; stored only as a salted local hash in normalized utterances.
- Utterance timestamp: the message `time.created` (preferred, present in all generations) or the
  SQLite `time_created` column; Unix epoch milliseconds, UTC. Missing/unparsable: utterance kept
  with no timestamp, reported `undated`.
- Utterance ID: deterministic hash of (adapter ID, session ID, message ID, ordered part IDs,
  text hash) per the shared hashing rules.
- App version: the session `version` field (writing app version) aggregated into the instance
  report as a range; never trusted for parsing decisions, which are per-record feature detection.

## 6. Instance model

One source instance per (storage root, projectID) pair. In SQLite, `project_id` comes from the
`session` rows (the `project` table is never read); in J2 from the `session/<projectID>/`
directory name; in J1 from the `<slug>` directory name (J1 pre-migration slugs and J2/S
git-root-derived project IDs may differ for the same project — instances are merged only by
record-ID dedup, never by path guessing). Instances get opaque labels (`OpenCode 1`, ...) with
candidate counts and date ranges; project IDs, slugs, directories, and titles are hashed or
omitted, never returned.

## 7. Deduplication considerations

1. Cross-generation duplicates (required by spec 4.7): the v1.2.0 migration preserved `ses_`,
   `msg_`, and `prt_` IDs when copying JSON records into SQLite and left the JSON files in
   place. Rule: utterances with the same (session ID, message ID) collapse to one canonical
   utterance; the SQLite copy wins over J2, and J2 over J1, when texts agree. When texts differ
   (partial migration bugs), keep the copy with more parts and raise a
   `generation_text_mismatch` diagnostic.
2. Channel databases: `opencode.db`, `opencode-prod.db`, and other `opencode-*.db` files may
   overlap after channel renames. The same ID rule applies across databases under one root.
3. J1a/J1b inline parts versus J1c split parts cannot coexist for one message after the in-place
   migration; if both shapes somehow appear, the split-part copy wins.
4. Compaction rewrites nothing in place: compaction summaries are `compaction` parts or v2
   `compaction` messages, both excluded, and original user messages remain.
5. Imported shared sessions (`opencode import`) are not distinguishable; exact-hash cross-source
   dedup plus the shared normalizer's authorship filters are the only mitigation (10.3).
6. Cross-source duplicates (dictation pasted into OpenCode) are handled by the shared normalizer
   (spec 4.8).

## 8. Safe discovery and snapshot behavior

Discovery (read-only, local, no network, no model):

- Resolve the root (2.1). Enumerate only allowlisted paths (2.2).
- SQLite stores: open with the Python `sqlite3` module in read-only URI mode
  (`file:...?mode=ro`) with a busy timeout; never `immutable=1` on a live database. Probe
  `sqlite_master` for the required tables and columns; on a probe failure report the store as
  `detected, unsupported schema`. Count candidates with aggregate queries over `session`,
  `message`, and `part` only. Read-only discovery queries must not run a checkpoint or create
  side files.
- JSON stores: stream-parse individual files; tolerate empty files, unknown keys, and unknown
  part types. A file that is not valid JSON is counted `malformed`, never fatal for the store.
- Return only `InstanceInventorySummary` aggregates; never text, paths, titles, or filenames.
- Performance: JSON message counting needs only `role` and part `type`/`synthetic` fields;
  SQLite counting is SQL-side. Date-range pre-filtering uses `time_created` indexes.

Snapshot (spec 3.6 preflight applies: containment, symlink, git-ignore, synced-root checks):

- SQLite: copy via the SQLite backup API (Python `sqlite3.Connection.backup`) into
  `<repository>/runtime/runs/<run-id>/snapshots/opencode/<instance-hash>/`, which yields a
  consistent snapshot of a live WAL database without touching the source. Immediately after the
  backup completes and before any extraction, sanitize the snapshot: `DROP TABLE IF EXISTS` for
  every never-read table in 2.3 (including `account`, `credential`, `session_share`, `event`,
  `project`), then `VACUUM`. The unsanitized backup transiently contains OAuth tokens; the
  snapshot file is written 0600 in 0700 directories, the sanitize step is part of the same
  operation, and extraction refuses to run against an unsanitized snapshot (marker file written
  only after sanitize succeeds).
- JSON generations: plain byte copies of allowlisted files, preserving relative layout. Files
  are written in place by the app (not append-only); a file that changes size between stat and
  copy is re-copied once, then skipped with a `concurrent_write` note.
- Snapshot manifest records source sizes, SHA-256 hashes, mtimes, and the sanitize marker.
  Extraction runs only against the snapshot. Never write, lock, checkpoint, or delete anything
  under the OpenCode root.

Retention notes for honest date ranges: OpenCode does not auto-delete session history (unbounded
growth is a known open issue), so long histories are normal; its internal file snapshots and logs
are pruned but are never read by this adapter.

## 9. Failure behavior when evidence is insufficient

- Database present but required tables/columns missing (future or unknown schema): that store is
  `detected, unsupported schema` with a diagnostic code; no guessing, no partial reads.
- Database locked or unreadable after bounded retries: store `inaccessible`; never elevate, never
  retry with different journal modes.
- Encrypted or corrupt database (`file is not a database`, integrity failure): `inaccessible`
  with diagnostic; never bypass encryption.
- JSON file that fails to parse: per-file `malformed` count. More than 10% malformed files in a
  generation store marks that store `unsupported_schema`.
- Message/part JSON matching no known sub-shape: excluded, counted `unrecognized_record`; if such
  records dominate a store, the store falls to `unsupported_schema`.
- Unknown part `type` or unknown v2-era table: ignored individually (forward-compatible), never
  extracted.
- Root exists with no allowlisted stores: `found` with zero instances (installed, no history).
- The adapter never modifies source data, and a sanitize failure deletes the snapshot and fails
  the snapshot step rather than proceeding.

## 10. Unresolved questions

Tracked for refresh before the release-candidate freeze:

1. v2 projection lifetime: whether a future release stops upserting `message`/`part` from the
   event-sourced layer. Required behavior now: fingerprint requires `message` and `part` tables
   with rows consistent with `session` count; a database where the v2 tables exist but v1 tables
   are empty while sessions exist is `detected, unsupported schema` until research is refreshed.
2. Slash-command template expansions (4). V1 behavior: extract them like other non-synthetic user
   text but flag utterances whose message follows a command execution when structurally visible;
   since no marker is persisted, the shared normalizer's copied/canned-material filters are the
   real gate, and known vendor template texts (for example the `/init` prompt) are excluded by
   exact match from a fixture-tested denylist. Precision loss is accepted.
3. Imported shared sessions (`opencode import`) are unmarked. V1 behavior: no structural
   exclusion is possible; rely on cross-source dedup and normalization. Documented residual risk:
   a user who imports another person's share and then audits contributes that person's text as
   candidates; the risk is mitigated only by the user's period/source selection and the
   authorship filters. Revisit if a marker lands upstream.
4. Windows fixture verification: path casing, `%USERPROFILE%` resolution, and WAL snapshot
   behavior on NTFS are unverified against a real install; required before the Windows gate.
5. Desktop app: whether it can point at a different data root in any released configuration.
   Behavior: only the standard root is discovered.
6. `opencode-<channel>.db` naming history: the exact release windows for `opencode-prod.db` are
   not fully mapped; the `opencode*.db` glob plus ID dedup makes the answer non-load-bearing.
7. Exact first version writing `synthetic` on injected parts: present since at least v0.3.x;
   earlier J1a records predate the part model. Injected text in pre-`synthetic` records is not
   structurally separable; those stores rely on the normalizer, and this is a documented
   precision risk for very old histories.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/opencode/<variant>/` with `fixture.json` metadata. All content is
synthetic; fake secrets are unmistakably fake (`sk-FAKEFAKEFAKE0000`). Databases are built by the
fixture generator at test time from committed SQL/JSON sources so no binary blobs are committed.

| Variant | Contents | Expected result |
|---|---|---|
| `success-sqlite` | `opencode.db` with WAL side files: 2 projects, parent sessions and one `parent_id` child, user messages with plain text parts, `synthetic: true` parts, `ignored: true` part, reasoning/tool/file parts, assistant messages, archived session, populated `account`/`credential`/`session_share`/`event` tables with fake secrets | only plain user text extracted; child session excluded; sanitize drops secret tables; opened-table audit clean |
| `success-json-j2` | `storage/` tree with `migration` marker `2`, sessions/messages/parts, one `parentID` child session, `session_share` and `todo` dirs present | J2 extracted; denylisted dirs never opened |
| `success-json-j1` | `project/<slug>/storage/` trees incl. `global`, J1a metadata-style message, J1b inline-parts message, J1c split parts | all three sub-shapes extracted or counted per 3.3 |
| `migration-overlap` | same records present in J1, J2, and SQLite with preserved IDs; one message whose SQLite copy lacks parts (migration bug) | ID dedup keeps one canonical copy; `generation_text_mismatch` raised for the partial message |
| `channel-dbs` | `opencode.db` plus `opencode-prod.db` with overlapping sessions | cross-database ID dedup; two stores, one instance set |
| `empty` | root with empty `storage/`, zero-row database, no `project/` | found, zero candidates, no errors |
| `malformed` | truncated JSON files above and below the 10% threshold; database with `message.data` invalid JSON rows | thresholding per 9; bad rows counted, store survives |
| `unsupported` | database missing the `part` table; database with sessions but empty v1 tables and populated `session_message`; unknown-layout JSON tree | `detected, unsupported schema` per store |
| `command-template` | user message whose text equals the fixture vendor-template denylist entry | excluded by exact match |
| `wsl-hint` | fake `/mnt/c/Users/<fake>/.local/share/opencode` root fed through path resolution | not discovered; hint diagnostic emitted |
| `denylist` | root with fake `auth.json`, `mcp-auth.json`, `opencode.jsonc`, `storage/session_share/` | opened-path audit proves none were read |

Platform axes: all variants parse identically on macOS, Linux, WSL, and native Windows runners;
path tests cover `XDG_DATA_HOME` override, `OPENCODE_DB` override, missing root, and unreadable
database. A private smoke test against at least one real installation per claimed platform is
required before stable release; its outputs are never committed.

## 12. Reproducible read-only inspection commands

Safe on a real installation (structure only, no content printed). Placeholder paths only.

```bash
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/opencode"

# Which generations exist (names only)
ls "$ROOT" | grep -E '^(opencode.*\.db|storage|project)$'
cat "$ROOT/storage/migration" 2>/dev/null

# SQLite: tables and session/message/part counts (no payload values)
python3 - "$ROOT/opencode.db" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")))
for t in ("session", "message", "part"):
    try: print(t, db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    except sqlite3.Error as e: print(t, "ERR", e)
print("user msgs", db.execute(
    "SELECT COUNT(*) FROM message WHERE json_extract(data,'$.role')='user'").fetchone()[0])
EOF

# JSON trees: layout census (file names only)
find "$ROOT/storage" -maxdepth 2 -type d 2>/dev/null | head
find "$ROOT/project" -maxdepth 4 -type d -name info 2>/dev/null | head

# Part-type census of one JSON store (field values are enums, no text printed)
python3 - "$ROOT/storage/part" <<'EOF'
import json, pathlib, collections, sys
c = collections.Counter()
for p in pathlib.Path(sys.argv[1]).rglob("prt_*.json"):
    try: o = json.load(open(p))
    except Exception: c["<bad>"] += 1; continue
    c[(o.get("type"), bool(o.get("synthetic")))] += 1
print(c.most_common())
EOF
```

Never run `cat`, `jq .`, or any query that prints `text`, `data`, or `value` contents from a real
store, and never open `auth.json` or query the `account`, `credential`, or `session_share`
tables.
