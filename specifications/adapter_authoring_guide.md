# Adapter authoring guide

A source adapter turns one local application's history into normalized, privacy-contained
records. Adding a source means adding an adapter package, its fingerprints, fixtures, verifier
rules, and documentation. This guide states what every adapter must do and what gates it must
pass.

Stable adapter IDs: `claude_code`, `codex`, `aider`, `gemini_cli`, `opencode`, `cline`,
`roo_code`, `wispr_flow`, `cursor` (beta). IDs are lowercase `[a-z][a-z0-9_]*` and are public
contract strings.

## 1. Responsibilities

Every adapter is version-aware and implements four operations:

```text
discover()   Local-only detection, parsing, and aggregate inventory
snapshot()   Consistent read-only snapshot
extract()    Explicit user/self records and original text fields
verify()     Adapter-specific structural and semantic checks
```

Rules:

- Adapters never modify source application data.
- Discovery is local-only and returns aggregates. Discovery code must never print, log, or
  return source text; the only per-instance shape shown to the agent is
  `InstanceInventorySummary`.
- Adapters accept structured verifier feedback and support bounded regeneration or
  re-extraction of local artifacts.

## 2. Mandatory research gate

No adapter code or production fixture may be created before its source specification at
`specifications/sources/<adapter-id>.md` has been written, reviewed, and accepted.

Research uses primary evidence wherever available: vendor documentation, vendor source code,
release notes, published schemas, and maintainers' issue discussions. Reverse engineering may
fill gaps but must be labeled as such and corroborated where possible. Every material claim
carries its source URL or repository path, publication or commit date when known, access date,
tested application version, and evidence strength.

Each source specification covers macOS, Windows, and Linux separately, even when the conclusion
is "unsupported." Never infer one platform's path or schema from another without evidence. For
every relevant platform and application generation, document:

- Supported, unsupported, untested, inaccessible, or not-applicable status.
- Default and configurable storage locations, including sandbox, container, roaming, native,
  WSL, and archived-data variants.
- File formats, schema fingerprints, tables, fields, record nesting, compression, encryption,
  WAL, and snapshot requirements.
- How user-authored records, roles, text parts, sessions, timestamps, and source versions are
  identified.
- Which field is original, pre-enhancement, cleaned, generated, injected, or of unknown
  provenance.
- Exact inclusion and exclusion rules, including credential and context-field denylists.
- Known schema migrations, historical formats, retention settings, corruption cases, and
  version boundaries.
- Safe discovery, snapshot, extraction, deduplication, and failure behavior.
- Unresolved questions and the exact behavior required when evidence is insufficient.
- A test matrix and synthetic fixture plan for every supported storage variant.

Include reproducible read-only inspection commands where safe. The specification must not
contain real transcript content, credentials, identifying local paths, or other private data.
Refresh the research before supporting a new storage generation or changing a parser because of
upstream behavior. When evidence stays insufficient, report "detected, unsupported schema"
instead of implementing a guess.

## 3. Normalized instance contract

`discover()` produces one `SourceInstanceRecord` per instance (private; the agent sees only the
derived `InstanceInventorySummary`). Fields, mirroring `artifacts/models.py`:

`adapter_id`, `adapter_version`, `instance_key`, `opaque_label`, `storage_format`,
`schema_fingerprint`, `path_hash` (SHA-256 of the canonical path; never the path itself),
`os_environment` (`macos | windows | wsl | linux`), `app_version` (nullable), `stability`,
`accessibility` (`found | not_found | unsupported_schema | inaccessible`), `diagnostic_code`
(nullable), `estimated_records`, `earliest_timestamp`, `latest_timestamp`, `candidate_messages`,
`candidate_words`, `candidate_bytes`.

`InstanceInventorySummary` carries the same aggregate fields but replaces path and workspace
metadata with `opaque_label`. It must never gain a field that reveals a path, project,
workspace, account, or any source text.

## 4. Normalized utterance contract

`extract()` produces `NormalizedUtterance` records. Fields, mirroring `artifacts/models.py`:

`utterance_id`, `source_adapter`, `adapter_version`, `session_hash`, `timestamp` (nullable),
`text`, `modality` (`written | spoken_asr | unknown`), `text_status`
(`verbatim | cleaned | unknown`), `authorship_confidence` (0.0-1.0), `authorship_basis`,
`source_path_hash` (local-only), `destination_app` (nullable, coarse), `content_flags`.

Only `verbatim` records enter the default high-confidence corpus. `cleaned` and `unknown`
records stay quarantined unless a future explicit policy admits them.

## 5. Authorship boundaries

Adapters perform structural attribution only:

- Keep explicit human/user role records.
- Use raw or original ASR fields, never enhanced or formatted fields.
- Exclude assistant, system, developer, tool, result, notification, and injected context
  records.
- Never read neighboring credential files.

The shared normalization layer — not the adapter — then removes copied, pasted, quoted,
generated, code-like, rewrite-target, and other non-authored material.

## 6. Database handling

SQLite and SwiftData-backed sources are read only from consistent snapshots:

- Use the SQLite backup API, or copy the main file together with its WAL and SHM files.
- Never treat a live database as an immutable flat file.
- Probe tables, columns, and JSON fields before querying.
- Return "detected, unsupported schema" (`SOURCE_UNSUPPORTED_SCHEMA`) instead of guessing.
- Never bypass encryption.

Snapshots are created only under `<repository>/runtime/runs/<run-id>/snapshots/` after the
path-safety preflight (containment, symlink, Git-ignore, and synced-root checks). Every snapshot
directory carries a cleanup manifest; cleanup deletes only files listed there.

## 7. Fixture requirements

- No real user transcript or mistake example is ever committed.
- Fixtures are synthetic and cover every supported storage generation.
- Secret-looking fixture values are unmistakably fake (for example `sk-FAKEFAKEFAKE0000`).
- Every adapter ships success, empty, malformed, unsupported-schema, and migration fixtures
  under `fixtures/<adapter_id>/<variant>/` with a `fixture.json` metadata file.

Adapter tests must prove:

- Local-only discovery returns no source content to the agent or network.
- Role and field allowlists exclude assistant, tool, and context data.
- Live database snapshot logic handles WAL safely.
- Schema drift fails closed.
- Source credential files are never opened.
- Cross-source duplicates resolve deterministically.
- macOS, native Windows, and every claimed WSL path variant are tested; native Linux variants
  whenever the adapter claims them.

## 8. Stability levels and release gates

Stability is the `Stability` enum: `stable`, `beta`, `experimental`.

- `experimental`: in development; not offered in a release build.
- `beta`: works on tested variants but with known provenance or coverage limits. Example:
  `cursor` treats text rawness as `unknown` unless a tested variant proves otherwise; an unknown
  variant may be inventoried but contributes no analyzable text.
- `stable`: eligible for default use in a release.

Release gates for a `stable` adapter:

- Accepted source specification at `specifications/sources/<adapter-id>.md` (Section 2).
- A published compatibility matrix naming the tested application version, storage fingerprint,
  operating-system environment, and raw-field provenance.
- Synthetic fixtures for every supported storage generation, plus a private smoke test against
  at least one real installation per claimed platform/storage variant. Smoke-test source content
  and outputs are never committed.
- All Section 7 tests green, plus the repository quality gate (ruff, mypy strict, pytest,
  schema-export check).

V1 supports the latest stable Codex and Claude Code runtime available when the release candidate
is frozen. Additional supported runtime versions are listed explicitly; "latest" is not a
permanent compatibility promise after release.
