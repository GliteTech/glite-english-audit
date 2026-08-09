# Adapter compatibility matrix

Release rule (project specification, 13.2): every stable adapter ships with the tested
application version, storage fingerprint, operating-system environment, and raw-field provenance.
Synthetic fixtures are supplemented by a private smoke test against at least one real
installation for every claimed platform/storage variant; smoke-test content is never committed.

Status legend:

- **verified** — synthetic fixtures pass and a real-installation smoke test ran on that platform.
- **fixtures-only** — synthetic fixtures pass; the real-installation smoke test is still
  required before that platform/variant can be claimed in a release.
- **fail-closed** — the adapter deliberately refuses this environment and tells the user why.

## Current status (2026-08-09, pre-release)

| Adapter | Stability | Storage variant (fingerprint) | macOS | Windows | Linux | WSL | Raw-field provenance |
|---|---|---|---|---|---|---|---|
| `claude_code` | stable target | project JSONL `v2-current` (app 2.1.201–2.1.226 observed) | **verified** (real smoke 2026-08-08: 53 instances, 1,094 messages extracted) | fixtures-only | fixtures-only | fixtures-only | `type=user` human-origin `message.content`; wrappers stripped |
| `claude_code` | — | legacy generation | fixtures-only (migration fixture) | fixtures-only | fixtures-only | fixtures-only | as documented in the source specification |
| `codex` | stable target | rollout JSONL `legacy-events+response-items` | **verified** (real smoke 2026-08-08: 6,655 messages discovered; August subset extracted) | fixtures-only | fixtures-only | fixtures-only | user message events per the source specification |
| `aider` | **beta** | `input-history-v1`, `chat-markdown-v1` fallback | fixtures-only | fixtures-only | fixtures-only | fixtures-only | prompt_toolkit input history entries; `/`- and `!`-entries excluded |
| `gemini_cli` | **beta** | session JSON (monolithic) + JSONL generations | fixtures-only | fixtures-only | fixtures-only | fail-closed for Windows-host stores | `type=user` records; `displayContent` preferred; tool-injected pseudo-user records excluded |
| `opencode` | stable target | SQLite `sqlite` (v1.18.15 observed) | **verified** (real smoke 2026-08-08: live WAL database snapshotted and 5 utterances extracted, no diagnostics) | fixtures-only | fixtures-only | fixtures-only | text parts not marked synthetic/ignored; subtask sessions excluded |
| `opencode` | — | J2 flat JSON, J1 project trees | fixtures-only (migration fixtures) | fixtures-only | fixtures-only | fixtures-only | as documented in the source specification |
| `cline` | **beta** | per-task API history (versioned names) | fixtures-only | fixtures-only | fixtures-only | fixtures-only | API history user messages; `<task>`/`<feedback>` wrappers stripped |
| `roo_code` | **beta** | per-task API history (G1/G2 generations) | fixtures-only | fixtures-only | fixtures-only | mounted host stores hinted, not read | API history user messages; wrapper conventions stripped |
| `wispr_flow` | stable (macOS verified) | `flow.sqlite` History table (63 columns observed) | **verified** (real smoke 2026-08-08: fingerprint matched, live database snapshotted, 12 utterances extracted as `spoken_asr`/`verbatim`; a hash comparison confirmed every extracted value came from `asrText` and none from `editedText`) | fixtures-only | not applicable | **fail-closed** (native Windows required) | `asrText` only; every other column never ingested |
| `cursor` | **beta** | `state.vscdb` G4 (`composer_v=10-16;bubble_v=3`) | **verified** (real smoke 2026-08-08: 663 composers, 4,442 messages inventoried; zero text extracted by design) | fixtures-only (inventory) | fixtures-only (inventory) | fixtures-only (inventory) | stored prompt reconciled against the editor state; mention tokens stripped |

## Why five adapters are beta

An adapter is stable only once someone has watched it run against a real installation of its
application, not merely once its tests pass against synthetic fixtures. Aider, Gemini CLI, Cline,
and Roo Code are implemented and tested but none of those applications exists on any machine
available to this project, so their user experience is unobserved. Beta means never selected by
default: a user can still choose one deliberately.

Cursor is the fifth, and it is beta for a different reason. Its macOS smoke test on 2026-08-09
inventoried a real store but extracted no text, so it did not test the one thing the project
specification (1.4, 4.7) requires before promotion: proof that a variant stores the prompt raw.
Only G4 on macOS carries that proof today.

Wispr Flow graduated on 2026-08-09 after a real-installation smoke test on macOS. It remains
untested on every other platform, which the rows above record; its adapter fails closed on an
unrecognized store rather than guessing, which is what makes a macOS-only stable claim defensible.

Promoting any of the four unobserved adapters needs one real installation, one smoke test, and
one recorded run — not more code.

## What blocks release claims

- No Windows, native Linux, or WSL environment has run any real smoke test yet. Until then, V1
  cannot claim those platforms for any adapter (specification, 13.9).
- `wispr_flow` is claimed stable on the strength of its macOS smoke test alone. The native
  Windows smoke test its source specification requires is still outstanding, and until it runs
  no Windows release claim may rest on this adapter.
- `cursor` stays beta until a tested variant beyond macOS G4 proves raw provenance. On every
  unproven variant it inventories and extracts nothing.
- Real-installation smoke tests for `aider`, `gemini_cli`, `cline`, and `roo_code` require
  machines with those applications installed; none is present on the reference machine.

Reference machine for every smoke test above: macOS 15 (Darwin 24.6.0), Apple Silicon,
Python 3.12, run 2026-08-08.

Smoke-test results referenced here are aggregate numbers only; no source content leaves the
machine where a smoke test runs.
