# Frozen submission-contract versions

The submission contract is implemented specification-first in this repository and adopted by the
website afterwards (specification workflow, Section 11.1). A frozen version never changes: a
change to any model that alters a generated schema requires a new
`submission_schema_version` and a new row here. A sync test recomputes the schema digests and
fails when a frozen row no longer matches the generated files.

## Version 1 — frozen 2026-08-08

Status: frozen for the public-repository milestone. Direct submission stays disabled until a
website advertises compatibility with version 1; until then the local review page is
download-only.

Authoritative models: `src/glite_english_audit/artifacts/submission.py`
(`SUBMISSION_SCHEMA_VERSION = 1`). Generated JSON Schemas and their SHA-256 digests at freeze:

| Schema file | SHA-256 |
|---|---|
| `schemas/new_submission_request.schema.json` | `89a515b5c35aaabeb404b38fdbd3ceba9966dc7cd455504a297a7be47bb46ffb` |
| `schemas/report_lookup_request.schema.json` | `dc5546b3dda23c5b67b26c091acd8f432be9f8f0dd73502e4bbf60340d9f6d68` |
| `schemas/submission_accepted.schema.json` | `5e928e149e23d4a008d57a02598f02955591e98e1fd4e9b262ba4cac1a44c0ee` |
| `schemas/submission_package.schema.json` | `bb921e4453a0a1d68f43afec1384252214e92f33c61a3f69421b900c66502239` |
| `schemas/submission_rejected.schema.json` | `b7745a4ee44ec4400b261f07d239de5f5d6ff572711b3b6ad0764408ffb7309c` |

Version-1 semantics the website must implement: the Section 8.3 allowlist, the canonical payload
hash (sorted keys, compact separators, UTF-8, `payload_hash` excluded), idempotent resubmission
returning the existing state, same-ID/different-hash conflict, wrong-secret indistinguishable
from unknown ID, one-way recovery-secret storage, and the consent envelope with three literal
`true` attestations plus the consent-policy version.
