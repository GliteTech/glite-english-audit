# Source specification: Wispr Flow (`wispr_flow`)

Status: source specification for the `wispr_flow` adapter (spec section 4.2 research gate),
awaiting review.
Adapter ID: `wispr_flow`. Stability: stable. The macOS real-installation smoke test recorded in
`specifications/compatibility_matrix.md` confirmed the schema fingerprint on 2026-08-09. The
native Windows smoke test the section 9 gates require is still outstanding, and the adapter
fails closed on any fingerprint mismatch on every platform.
Research log: `temp/findings/wispr_flow-source-research.md` (evidence IDs E1–E13 cited below).
Access date for all cited evidence: 2026-08-08.

Wispr Flow is a closed-source Electron dictation application (macOS and Windows desktop). It
persists every dictation as one row in a local SQLite database. Wispr Flow was NOT installed on
the research machine: everything below is vendor documentation plus labeled third-party reverse
engineering (working open-source integrations and app-teardown write-ups). Nothing in this file
is direct structural observation by this project. That is why every extraction rule is gated by
a strict fingerprint check and unknown shapes fail closed to "detected, unsupported schema".

V1 hard requirement (project spec 4.7): accept ONLY the raw `asrText` field. Never ingest
formatted, edited, clipboard, window, accessibility, audio, screenshot, or context fields.
`modality = spoken_asr` and `text_status = verbatim` apply to `asrText` and to no other column.

## 1. Platform status and storage locations

Never infer one platform's behavior from another. Each row carries its own evidence.

| Platform | Status | Data root | Evidence |
| --- | --- | --- | --- |
| macOS (12+, ARM and Intel) | Supported, fingerprint unverified against a real install | `~/Library/Application Support/Wispr Flow/` | E5, E6, E8 (moderate) |
| Windows (10/11, x64 only) | Supported, fingerprint unverified against a real install | `%APPDATA%\Wispr Flow\` (Roaming) | E1 (strong, vendor), E5 (moderate) |
| Linux (native) | Not applicable — no official application | — | E3, E4 (strong) |
| WSL | Fail closed — direct the user to native Windows | Windows-host data visible at `/mnt/<drive>/Users/<user>/AppData/Roaming/Wispr Flow/` | E1, E4 + spec 1.3 |

Details:

- The primary artifact is the SQLite database `flow.sqlite` directly inside the data root on
  both platforms (E5, E6, E8). No environment-variable override of this location is documented;
  discovery checks only the default root. A missing root means `not_found`.
- macOS: the app is a normal (non-App-Store, non-sandboxed) Electron app with a Swift helper
  (E8). No `~/Library/Containers` variant is documented; discovery does not probe for one.
  Reading another app's `Application Support` folder can be blocked by TCC in some setups;
  the Raycast integration notes Full Disk Access may be required (E5). A permission error is
  `inaccessible`, never retried with elevation.
- Windows: per-user install under `%LOCALAPPDATA%\WisprFlow\app-<version>\` (Squirrel layout,
  E5, E11); user data in `%APPDATA%\Wispr Flow\` (E1, E5). ARM Windows is unsupported by the
  vendor (E3). The install directory is never opened by the adapter.
- Linux: the vendor states Flow "is not available as a native Linux application" (E4) and lists
  Linux as unsupported (E3). An unofficial community repack exists (E12). V1 does not discover
  it: if a user points a future override at such a store, any non-matching fingerprint reports
  `detected, unsupported schema`; a matching fingerprint is still an untested variant and stays
  ineligible for the stable gate.
- WSL: Wispr Flow itself never runs inside WSL (it types into Windows apps; the vendor's WSL
  guidance is about pasting into WSL terminals, E4). The only data reachable from WSL is the
  Windows-host `flow.sqlite` through DrvFS. That is a live SQLite database owned by a
  long-running Windows process; consistent snapshotting (backup API, WAL/SHM semantics, file
  locking) is not established over DrvFS. Per project spec 1.3 this adapter fails closed in
  WSL: when `/mnt/<drive>/Users/<user>/AppData/Roaming/Wispr Flow/flow.sqlite` exists,
  discovery reports the instance as `inaccessible` with diagnostic
  `SOURCE_WSL_HOST_STORE_HINT`, contributes no counts and no text, and the agent
  directs the user to run the audit from native Windows.
- Mobile (iOS/Android) apps exist (E3) but their storage is out of scope and unreachable;
  not applicable.
- Retention shaping what discovery sees (E2, E9, E10):
  - Local history is device-local, not synced between devices, and destroyed by the in-app
    "Reset & restart" or by manual deletion of the data folder (E1, E9).
  - An enterprise/local data policy can be set to "Delete after 24 hours" or "Never store"
    (E10). A present, valid, but empty or short-history database is therefore normal:
    report `found` with low counts, never an error.
  - Cloud-side retention toggles (Privacy Mode, Private Cloud Sync, 14-day cloud audio window)
    do not change the local schema (E2, E7).

## 2. Files and secrets that must never be opened

The adapter opens exactly one file (plus its SQLite sidecars during snapshot):

```text
<data root>/flow.sqlite            the only allowlisted file
<data root>/flow.sqlite-wal        copied only as part of a consistent snapshot
<data root>/flow.sqlite-shm        copied only as part of a consistent snapshot
```

Everything else is denylisted. Explicitly forbidden (E6, E8 plus Electron layout):

- `config.json` in the data root — app configuration; may reference account/session data (E6).
- `SharedStorage`, `SharedStorage-wal` — Electron storage (E6).
- `backup-<timestamp>.sqlite` automated database backups (E6). They duplicate `flow.sqlite`
  content (dedup hazard) and their generations are unverified. Never opened in V1 (section 9).
- All Electron profile content in the data root: `Local Storage/`, `Session Storage/`,
  `IndexedDB/`, `Cookies*`, `Cache/`, `Code Cache/`, `GPUCache/`, `Preferences`,
  `blob_storage/`, `Crashpad/`, and any other file or directory not on the allowlist. Unknown
  entries are ignored silently and their names are never returned to the agent.
- Log directories: `~/Library/Logs/Wispr Flow/` on macOS (`main.log`, `accessibility.log`
  contain keystroke and lifecycle traces, E8); any `logs/` directory under the Windows data
  root (untested). Logs are never read.
- The bundled MCP package (`wispr-flow.mcpb`) and application install directories (E7).
- Credential stores: desktop auth tokens live in the app's private storage, with OAuth/SSO
  material app-layer encrypted (E10, vendor). As an Electron app, key material is expected in
  the macOS Keychain (safeStorage item, typically named "Wispr Flow Safe Storage") and via
  DPAPI on Windows — expected layout, unverified (inference from Electron defaults). The
  adapter never invokes `security(1)`, never calls DPAPI, never enumerates Keychain items, and
  never parses any file for credentials. Encrypted values encountered inside allowlisted data
  are never decrypted (spec 4.6: never bypass encryption).

Within `flow.sqlite`, the never-query denylist is defined in sections 3–4: only the `History`
table is queried, and only through the fixed column allowlist. `Dictionary`, `polish`, `notes`,
`note_versions`, `note_images`, `meetings`, `meeting_versions`, `calendar_events`, `links`,
`snippets`, `notifications`/`RemoteNotifications`, and any unknown table are never read for
content. `sqlite_master` and `SequelizeMeta` may be read for structure only (section 6.1).

## 3. Database schema

All schema knowledge is labeled reverse engineering (E5 working integration code, E6 schema
dump, E7 migration teardown of v1.5.308, E8 forensic dump of v1.4.752), corroborated across
four independent sources but never observed directly by this project. Strength: moderate.

### 3.1 Store shape

- Single SQLite database `flow.sqlite`, unencrypted (readable by stock `sqlite3`; vendor relies
  on FileVault/BitLocker for at-rest protection — E5, E10). Managed by Sequelize ORM over
  better-sqlite3 with 55 migrations as of app 1.5.308 (E7).
- Journal mode is undocumented. The adapter must handle both rollback-journal and WAL stores;
  snapshot rules in section 6.2 cover both.
- Table names appear as `History`/`Dictionary` in working queries (E5, E6) and lowercase in
  migration-era listings (E7); SQLite matches case-insensitively and so must the adapter.
- `SequelizeMeta` records applied migration filenames — the best available generation marker.

### 3.2 `History` table (one row per dictation) — current generation (2024-05 through v1.5.x)

Columns the adapter READS (allowlist; all feature-detected, only starred ones required):

| Column | Type (reported) | Use |
| --- | --- | --- |
| `transcriptEntityId`* | VARCHAR(36) PK, UUID | Stable local utterance ID basis |
| `asrText`* | TEXT, nullable | THE ONLY text field ever extracted |
| `timestamp`* | DATETIME as text | Utterance timestamp (section 5) |
| `app` | VARCHAR(255), nullable | Coarse destination app (bundle ID / app ID), local-only |
| `language` | TEXT, nullable | Language hint for candidate counting |
| `conversationId` | VARCHAR(255), nullable | Session grouping (section 5) |
| `status` | VARCHAR(255), nullable | Row-state flagging only; never a text source |
| `isArchived` | TINYINT(1), nullable | Content flag only; archived rows remain candidates |
| `appVersion` | TEXT, nullable | Detected application version for the instance report |
| `numWords`, `duration` | INTEGER / FLOAT | Cross-checks in verify(); never authoritative |

Columns that exist and are NEVER selected, read, logged, or returned (never-ingest denylist;
union of E6/E7/E8 observations — the real set may be larger, and unknown columns are equally
forbidden because the adapter selects only allowlisted columns by name):

- Enhanced/generated text: `formattedText`, `editedText`, `toneMatchedText`,
  `defaultFormattedText`, `fallbackFormattedText`, `defaultAsrText`, `fallbackAsrText`
  (A/B ASR variants are not the accepted raw field), `pastedText`.
- Clipboard/window/accessibility context: `textboxContents`, `axText`, `axHTML`,
  `additionalContext`, `url`.
- Audio and imagery: `audio`, `builtInAudio`, `screenshot`, `opusChunks`.
- Learning/behavioral metadata: `userEditMetaData`, `toneMatchPairs`,
  `personalizationStyleSettings`, `feedback`, `editedTextStatus`, `editedTextAttempts`,
  `numWordsCorrected`, `numDictionaryReplacements`.
- Operational: `needsUploading`, `shareType`, `micDevice`, `e2eLatency`, `speechDuration`,
  `averageLogProb`, `formattingDivergenceScore`, `usedFallbackAsr`, `usedFallbackFormatting`,
  `timezoneOffsetMinutes`, and any column not on the read allowlist.

Observed `status` values (E6, not exhaustive): `formatted`, empty string/NULL, `empty`,
`no_audio`, `dismissed`, `extension_paste`, `extension_other`. Unknown values do not exclude a
row (inclusion is decided by `asrText` alone, section 4) but are counted as
`unknown_status_value` diagnostics for research refresh.

### 3.3 Other tables (never content-read)

For fingerprinting awareness only: `Dictionary` (custom vocabulary/snippets; user words but not
utterances), `polish` (since ~2026-01: LLM rewrite history including instruction text),
`notes` + `note_versions` + `note_images` (scratchpad), `meetings` + `meeting_versions`
(meeting transcripts with speakers — multi-speaker, NOT the learner's verified speech),
`calendar_events`, `links` (clipboard URL tracking), `snippets`, `notifications` /
`RemoteNotifications`, `SequelizeMeta` (E6, E7). Their presence/absence contributes to the
fingerprint; their rows are never selected. Scratchpad notes and meeting transcripts are
explicitly out of scope for V1: provenance is mixed (AI transforms, other speakers) and the
spoken-vs-typed boundary is not recoverable.

### 3.4 Known schema history and boundaries

- Earliest known migrations: 2024-05 (`history`, `dictionary`) (E7). No pre-2024 generation is
  documented; any store missing the required `History` columns is unsupported, not guessed at.
- Table additions dated by migration era (E7): `notifications` 2025-04, `notes` 2025-05,
  `snippets` 2025-08, `polish` 2026-01, `note_versions`/`note_images` 2026-03, `meetings`,
  `meeting_versions`, `calendar_events`, `links` 2026-04. Extra tables never invalidate the
  fingerprint; missing required `History` columns always do.
- Tested-by-evidence app versions: 1.4.752 (E8, 2026-04) and 1.5.308 (E7, 2026-05). This
  project has tested no version directly (section 9).

## 4. Provenance of text fields and inclusion rules

### 4.1 Provenance ruling

- `asrText` — raw server-side ASR output of the user's speech, before AI formatting
  ("original transcription… before Wispr's formatting", E5 UI; "Original ASR text", E6;
  formatting/polish are separate later passes, E7). This is the only pre-enhancement field.
  Ruling: user-authored, `modality = spoken_asr`, `text_status = verbatim`. Note: verbatim
  here means "as recognized"; ASR mis-recognitions are possible and are handled by the
  analysis-stage rules (spec 7.5), not by the adapter.
- `formattedText`, `toneMatchedText`, `defaultFormattedText`, `fallbackFormattedText`,
  polish outputs — AI-generated or AI-cleaned: never eligible.
- `editedText` — post-hoc user edit of AI-formatted text; mixed provenance (edit of a cleaned
  artifact, possibly touched by extraction pipelines): never eligible in V1.
- `defaultAsrText`/`fallbackAsrText` — parallel ASR candidates; the spec 4.7 contract names
  `asrText` only, and the losing candidate was never accepted by the user: never eligible.
- `pastedText`, `textboxContents`, `axText`, `axHTML`, `additionalContext`, `url` — other
  people's/apps' content and screen context: never eligible, never read.
- `Dictionary`/`snippets` phrases — user-curated wordlists, not natural utterances: excluded.
- `meetings.transcript` — includes other speakers; excluded.

### 4.2 Row inclusion — a `History` row yields exactly one candidate utterance iff

1. `asrText` is present, non-NULL, and non-empty after Unicode-aware trimming.
2. `transcriptEntityId` parses as a UUID string (fallback: any non-empty unique PK value is
   accepted with diagnostic `nonuuid_pk`; duplicate PK values fail the file per section 8).

That is the whole rule. `status`, `isArchived`, `app`, `duration`, and `language` never gate
inclusion; they only annotate:

- `isArchived = 1` → `content_flags += ["archived"]`.
- `status` in `{dismissed}` → `content_flags += ["dismissed"]` (spoken but discarded by the
  user; still user speech).
- `status` in `{extension_paste, extension_other}` → `content_flags += ["command_mode"]`
  (the utterance was an instruction to Flow; still user speech, and the downstream
  normalization layer decides whether instruction-style utterances stay in the corpus).
- `app` present → `destination_app` = coarse app name derived from a fixed local bundle-ID map
  (unmapped IDs → `other`); local-only, never exported (spec 8.3).
- Rows whose `asrText` is empty are counted per status for the inventory (`empty_asr_rows`)
  but produce nothing.

Extraction metadata per kept utterance: utterance ID = hash of (adapter ID, `transcriptEntityId`,
text hash) per `artifacts/hashing.py`; session hash per section 5; timestamp per section 5;
`modality = spoken_asr`; `text_status = verbatim`; `authorship_confidence` basis
`sole_dictation_field` (the field cannot contain assistant output in any observed flow);
`source_path_hash` = SHA-256 of the canonical `flow.sqlite` path.

### 4.3 Exclusion summary (never contribute text)

Every column in the 3.2 denylist; every table in 3.3; every row failing 4.2. There are no
role fields in this source: exclusion is column-based, which is why the SELECT statement must
name allowlisted columns explicitly and must never use `SELECT *`.

## 5. Sessions, timestamps, deduplication

- Session: Wispr Flow has no session concept for dictation; each row is one dictation event.
  `session_hash` = salted local hash of `conversationId` when non-empty (groups related
  dictations), else of `transcriptEntityId` (single-utterance session). `conversationId`
  semantics are only partially documented (section 9); grouping is best-effort and never
  affects inclusion.
- Timestamps: `timestamp` column, written as `YYYY-MM-DD HH:MM:SS.mmm +00:00` (UTC) by
  observed integrations (E5). The adapter parses that form plus ISO-8601 variants
  (feature-detected). Unparsable or missing → utterance kept, reported `undated`, excluded
  from bounded period filters (spec 2.3: `None`, never zero). Earliest/latest instance
  timestamps = min/max over parsable kept rows.
- Within-store duplicates: `transcriptEntityId` is the primary key, so true row duplicates
  cannot occur in one store. Re-dictation of the same sentence minutes apart is genuine
  repeated language and must NOT be collapsed (spec 4.8). Rows sharing `conversationId` are
  distinct utterances, not duplicates.
- `backup-*.sqlite` files would duplicate the main store wholesale; they are never opened, so
  no dedup pass is needed for them in V1.
- Cross-source dedup is the mandated case for this adapter (spec 4.8): text dictated via Wispr
  Flow and pasted into a coding agent seconds later appears in both stores. The adapter only
  supplies text hashes, timestamps, and `destination_app`; the shared normalizer selects the
  canonical copy. Positive matching also upgrades the coding-agent copy's modality accounting
  per spec 5.5 (`spoken_asr` wins for matched events).

## 6. Discovery, snapshot, extraction, verification

### 6.1 discover()

1. Resolve the platform data root (section 1). WSL: apply the fail-closed rule before anything
   else. Missing root or missing `flow.sqlite` → `not_found`.
2. Preflight: confirm `flow.sqlite` is a regular file, readable, with SQLite magic header. A
   header that is not SQLite (or an encrypted/corrupt store) → `unsupported_schema` with
   diagnostic `SOURCE_UNSUPPORTED_SCHEMA`; permission failure → `inaccessible`.
3. Take a discovery snapshot (6.2) — discovery also never queries the live database — then
   open the snapshot read-only (`mode=ro` URI).
4. Fingerprint (structure reads only): `sqlite_master` table and column listing
   (case-insensitive), presence of required `History` columns
   (`transcriptEntityId`, `asrText`, `timestamp`), optional allowlist columns present,
   known/unknown extra tables, `SequelizeMeta` migration count and latest migration name.
   Missing required column or missing `History` table → `detected, unsupported schema`
   (`SOURCE_UNSUPPORTED_SCHEMA`), no counts, no guessing.
5. Inventory using only allowlisted columns: candidate rows (4.2), candidate word and byte
   counts computed locally from `asrText` with the project tokenizer (never from `numWords`),
   min/max timestamps, per-status empty-row counts, `appVersion` range. `asrText` values are
   parsed in-process and never printed, logged, or returned (spec 2.3).
6. Return one `SourceInstanceRecord` per store (normally exactly one per machine) with opaque
   label (`Wispr Flow 1`), `storage_format = sqlite`, fingerprint, hashed canonical path, and
   counts. One instance only; there are no projects/workspaces in this source.

### 6.2 snapshot()

`flow.sqlite` is a live database owned by a long-running app (menu-bar/tray resident). Per
spec 4.6 it is never treated as a flat file:

1. Preflight the snapshot directory (`<repository>/runtime/runs/<run-id>/snapshots/wispr_flow/`)
   per spec 3.6 (containment, symlink, git-ignore, cloud-sync checks).
2. Preferred: Python `sqlite3` backup API from a read-only (`mode=ro`) source connection to a
   fresh snapshot file. This yields a consistent copy regardless of journal mode and never
   writes to the source. Set a short busy timeout; on persistent `SQLITE_BUSY`/locked, retry
   bounded times, then fall back to (3).
3. Fallback: byte-copy `flow.sqlite` + `flow.sqlite-wal` + `flow.sqlite-shm` (those that
   exist), then verify the copy opens and `PRAGMA integrity_check` passes on the snapshot; a
   failed check discards the copy and retries once before reporting `SOURCE_LOCKED`.
4. Snapshot manifest records source sizes, mtimes, SHA-256 of copies, journal mode observed,
   and the cleanup manifest. Snapshot files are `0600` in `0700` directories.
5. The adapter never locks the live DB exclusively, never runs `PRAGMA wal_checkpoint`, never
   writes, and never opens the live store read-write. WSL never reaches this step (section 1).

### 6.3 extract()

Runs only against the snapshot. Single statement shape (columns feature-detected, never `*`):

```sql
SELECT transcriptEntityId, asrText, timestamp, app, language,
       conversationId, status, isArchived, appVersion
FROM History
WHERE asrText IS NOT NULL AND TRIM(asrText) != '';
```

Optional columns absent in an older-but-supported store are dropped from the SELECT, not
defaulted. Emits `NormalizedUtterance` records per section 4.2 metadata. No other table is
queried.

### 6.4 verify()

Adapter-specific deterministic checks: every utterance maps to a snapshot row satisfying 4.2;
no utterance text is empty; extracted count equals the discovery candidate count for the same
snapshot; no denylisted column name appears in any executed SQL (audit of the statement log);
no file outside the section 2 allowlist was opened (opened-path audit); `numWords`-vs-local
word-count divergence beyond 3x on more than 10% of rows raises `WORDCOUNT_DIVERGENCE` (a
research-refresh signal, not a failure); timestamps are monotonically consistent with the
reported range.

## 7. Candidate counting notes

- Candidate word counts use the project tokenizer on `asrText` only (spec 5.6); `numWords` is
  a cross-check because its definition (which text variant it counts) is unverified.
- `language` (when present) feeds the language-hint split in the inventory; the authoritative
  English filter is the shared stage-3 layer, not this column.
- Rows flagged `command_mode`/`dismissed`/`archived` stay in candidate counts; the shared
  normalization layer owns any exclusion decision, and the flags travel with the utterance.

## 8. Failure behavior

- Root or DB missing → `not_found`. Present but unreadable → `inaccessible`
  (`SOURCE_INACCESSIBLE`; on macOS the diagnostic text may mention Full Disk Access).
- Non-SQLite or encrypted file at the DB path → `unsupported_schema`
  (`SOURCE_UNSUPPORTED_SCHEMA`); never attempt decryption.
- Missing `History` table or any required column → `detected, unsupported schema`
  (`SOURCE_UNSUPPORTED_SCHEMA`); no extraction, no guessing (spec 4.2).
- Snapshot cannot be made consistent (backup API fails and fallback copy fails integrity) →
  `SOURCE_LOCKED`, instance skipped for this run; the live store is never used
  directly.
- Row-level anomalies: NULL/duplicate `transcriptEntityId` → affected rows excluded with
  `pk_anomaly` counters; if more than 1% of candidate rows are anomalous, the store falls to
  `unsupported_schema` (systematic shape mismatch).
- Unknown `status` values, unknown extra columns, unknown extra tables: tolerated, counted,
  never parsed beyond names.
- SQLite-level corruption reported by `integrity_check` on the snapshot → discard snapshot,
  report `SOURCE_UNSUPPORTED_SCHEMA`; never run recovery tooling against the live file.
- WSL detection of a Windows-host store → `SOURCE_WSL_HOST_STORE_HINT`
  (section 1); zero counts, zero text.
- The adapter never writes to, locks, truncates, repairs, or deletes anything under the data
  root, and never triggers the app's own reset/delete functions.

## 9. Unresolved questions and required behavior when evidence is insufficient

1. Partial real-installation verification: the macOS smoke test on 2026-08-09 confirmed the
   fingerprint, column allowlist, and timestamp format; the native Windows schema is still
   third-party evidence only. Required behavior: the fingerprint (6.1) is mandatory before any
   extraction; any mismatch is `detected, unsupported schema`. No Windows release claim may
   rest on this adapter until its Windows smoke test runs.
2. Windows schema parity: all deep reverse engineering is macOS; Windows parity is implied by
   cross-platform integrations (E5) only. Required behavior: identical fingerprint rules; the
   Windows smoke test is a separate gate, and Windows-specific drift falls to
   `unsupported_schema`, not to reuse of macOS assumptions.
3. `flow.sqlite` journal mode (WAL vs rollback) is undocumented. Required behavior: the 6.2
   backup-API-first snapshot handles both; the observed mode is recorded in the snapshot
   manifest for research refresh.
4. Whether `asrText` already reflects user-dictionary/snippet replacements or other ASR-side
   biasing. Current ruling stands (`verbatim` raw ASR field per E5/E6/E7); if refresh research
   shows server-side rewriting beyond recognition, the ruling must be revisited before stable.
5. `conversationId` semantics (grouping rules, lifetime) are undocumented. Required behavior:
   used only for the session hash; never for inclusion, ordering, or dedup.
6. Full `status` value set is unknown. Required behavior: statuses never gate inclusion;
   unknown values are counted (`unknown_status_value`) for research refresh.
7. Pre-2024 or early-beta store formats: no evidence they exist in the field. Required
   behavior: covered by the fingerprint fail-closed rule; no legacy parser is written without
   evidence.
8. `backup-*.sqlite` ingestion (could recover history deleted from the main store): out of
   scope for V1; files stay on the never-open list. Revisit only with explicit policy and
   dedup design.
9. Timestamp timezone edge cases (`timezoneOffsetMinutes` column suggests local-offset
   awareness): unverified. Required behavior: trust the `+00:00`-suffixed text form; rows with
   other offsets are parsed with their stated offset and normalized to UTC; unparsable →
   `undated`.
10. macOS TCC boundary for reading another app's Application Support data from the audit
    process: environment-dependent. Required behavior: surface `inaccessible` with actionable
    diagnostic; never instruct the user to grant broader permissions than reading requires.

## 10. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/wispr_flow/<variant>/` with a `fixture.json` metadata file. All
database fixtures are generated by a committed builder script (synthetic DDL mirroring section
3.2); no real store or transcript is ever committed; fake secrets are unmistakably fake.

| Variant | Contents | Asserts |
| --- | --- | --- |
| `success-current` | Full-column `History` with: plain dictations; a row with `formattedText`/`editedText`/`textboxContents`/`axText`/`url` populated with sentinel strings; `audio` BLOB bytes; statuses `formatted`, `dismissed`, `extension_paste`; one archived row; `conversationId` groups; `Dictionary`, `polish`, `notes`, `meetings`, `links` tables with sentinel rows; `SequelizeMeta` | Only `asrText` values extracted; no sentinel from any denylisted column/table appears in any output or log; flags set correctly |
| `success-minimal` | `History` with only the required + a subset of optional columns (older-generation simulation); no post-2025 tables | Optional columns feature-detected; extraction succeeds; fingerprint reports the reduced variant |
| `empty` | (a) valid store, zero `History` rows; (b) rows with only empty/NULL `asrText`; (c) data root without `flow.sqlite` | found-empty vs not-found; `empty_asr_rows` counted; retention-policy emptiness is not an error |
| `malformed` | (a) non-SQLite bytes at `flow.sqlite`; (b) truncated store failing `integrity_check`; (c) NULL and duplicate `transcriptEntityId` rows above and below the 1% threshold | `SOURCE_UNSUPPORTED_SCHEMA`, `SOURCE_UNSUPPORTED_SCHEMA`, `pk_anomaly` handling and threshold fall-through |
| `unsupported` | (a) `History` missing `asrText`; (b) `History` renamed; (c) plausible-but-unknown schema (extra required-column rename) | `detected, unsupported schema`; zero extracted text |
| `wal-live` | Store in WAL mode with `-wal`/`-shm` present and uncheckpointed committed rows | Backup-API snapshot captures WAL content; fallback copy path also tested; source files bit-identical after snapshot |
| `timestamps` | Rows with `+00:00` text form, ISO-8601 form, non-UTC offset, empty, and garbage timestamps | Parsing matrix; `undated` handling; period-filter behavior |
| `denylist` | Data root with fake `config.json` (`sk-FAKEFAKEFAKE0000`), `SharedStorage`, `backup-2026-01-01T00-00-00.000Z.sqlite`, `Local Storage/`, logs | Opened-path audit proves only `flow.sqlite*` opened; backup DB never opened |
| `windows-roaming` | Same store under a simulated `<home>/AppData/Roaming/Wispr Flow/` layout | Path resolution on Windows; identical extraction |
| `wsl-failclosed` | Simulated `/mnt/c/Users/<fake>/AppData/Roaming/Wispr Flow/flow.sqlite` visible from a WSL-flagged environment | `SOURCE_WSL_HOST_STORE_HINT`; zero counts; native-Windows hint surfaced |
| `dedup-cross-source` | Wispr store plus a synthetic Claude Code fixture containing the same sentence seconds later | Shared normalizer selects one canonical utterance; Wispr copy wins `spoken_asr` modality |

Platform matrix: the suite runs on macOS, Linux (fixtures only — adapter reports not-applicable
for live discovery), native Windows, and WSL runners. Stable release additionally requires the
section 9.1 private smoke tests on real macOS and Windows installations; smoke outputs are
never committed.

## 11. Reproducible read-only inspection commands

For maintainers verifying a real installation during the smoke test. Run only against a
snapshot copy, never the live file. Structure and counts only — these commands never print
`asrText` or any other content column.

```bash
SRC="$HOME/Library/Application Support/Wispr Flow/flow.sqlite"   # Windows: %APPDATA%\Wispr Flow\flow.sqlite
cp "$SRC" /tmp/wf-inspect.sqlite 2>/dev/null   # plus -wal/-shm if present; or use .backup

DB="file:/tmp/wf-inspect.sqlite?mode=ro"

# Table inventory
sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"

# History column names and declared types (no values)
sqlite3 "$DB" "PRAGMA table_info(History);"

# Row counts, date range, status distribution (no text)
sqlite3 "$DB" "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM History;"
sqlite3 "$DB" "SELECT status, COUNT(*), SUM(asrText IS NOT NULL AND TRIM(asrText)!='') FROM History GROUP BY status;"

# Migration generation marker
sqlite3 "$DB" "SELECT COUNT(*), MAX(name) FROM SequelizeMeta;"

# Journal mode of the live store (harmless pragma, still run it on the copy)
sqlite3 "$DB" "PRAGMA journal_mode;"
```

Never run `SELECT *`, `.dump`, or any query naming `asrText`, `formattedText`, `editedText`,
`textboxContents`, `axText`, `axHTML`, `additionalContext`, `audio`, or `screenshot` as output
columns against a real store.
