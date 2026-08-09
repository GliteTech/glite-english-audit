# Agent Skills Specification

**Version**: 1

Adapted from the `glite-arf` agent skills specification (Apache-2.0).

---

## Purpose

This specification defines the shared format, discovery layout, and verification rules for skills
in `glite-english-audit`. It ensures that a single authored skill works in both Claude Code and
Codex without duplicating content and without symlinks, so a fresh clone behaves the same on
Windows, macOS, and Linux.

**Producer**: Human maintainers editing skills under `skills/`, plus the wrapper generator
`python -m glite_english_audit.verification.generate_wrappers`.

**Consumers**:

* **Claude Code** — discovers skills through `.claude/skills/`
* **Codex** — discovers skills through `.codex/skills/`
* **`verify_skills`** — the deterministic verifier
  (`python -m glite_english_audit.verification.verify_skills`) validates skills and wrappers
  against this specification
* **`check-skill`** — the semantic review skill references this specification when judging skill
  quality

---

## Canonical Location

Skills are authored once under:

```text
skills/<skill-name>/SKILL.md
```

`<skill-name>` is the stable skill slug and directory name. Use lowercase letters, digits, and
hyphens. Do not create separate authored copies for Claude Code and Codex, and do not author
anything directly under `.claude/skills/` or `.codex/skills/`.

---

## Discovery Layout: Generated Wrappers, Not Symlinks

Unlike `glite-arf`, this repository does not use symlinks for skill discovery. Symlinks require
Developer Mode or elevated permissions on Windows and silently degrade to plain text files
otherwise. Instead, both discovery directories contain generated wrapper files:

```text
.claude/skills/<skill-name>/SKILL.md   (generated)
.codex/skills/<skill-name>/SKILL.md    (generated)
```

Wrappers are produced by:

```bash
uv run python -m glite_english_audit.verification.generate_wrappers
```

A wrapper is: the canonical frontmatter verbatim, then:

```markdown
# <name> wrapper

Generated wrapper. Do not edit. Read and follow the canonical skill instructions in
`skills/<name>/SKILL.md` exactly.
```

The verbatim frontmatter lets both tools discover the skill by `name` and `description`; the body
routes the agent to the canonical instructions. Wrappers are committed to the repository.

Rules:

* Never edit a wrapper by hand. Edit `skills/<skill-name>/SKILL.md` and regenerate.
* Regenerate wrappers in the same change as any skill edit.
* `verify_skills` re-derives every wrapper from its canonical skill and fails on any byte-level
  difference (`SKILL_WRAPPER_DRIFT`) or missing wrapper (`SKILL_WRAPPER_MISSING`).

---

## File Format

Each canonical `SKILL.md` is a Markdown file with:

1. YAML frontmatter at the top
2. A Markdown body immediately after the closing `---`

### Required Frontmatter

The file must begin with a YAML frontmatter block containing:

* `name` — string, must match the skill directory slug
* `description` — string, must state what the skill does and when it should be used

Example:

```markdown
---
name: "find-english-mistakes"
description: "Read one session's projected utterances and answer with the
mistakes found in them, each already privacy-clean and addressed by the index
and span it was found at. Use during step d, one agent per session file."
---

# Find English Mistakes
```

### Optional Frontmatter

Tool-specific metadata may be added only when it is clearly needed and remains compatible with
both toolchains. Do not require Claude-only metadata for baseline skill validity. When a skill
needs runtime-specific behavior, isolate it in a clearly labeled body subsection and document why
the difference exists.

---

## Required Body Structure

The Markdown body must contain:

* exactly one `#` title heading
* `**Version**: N` near the top, using a plain integer (1, 2, 3 — never semver)
* `## Goal`
* `## Inputs`
* `## Context`
* `## Steps` — numbered, in execution order
* `## Done When`
* `## Forbidden`

Additionally required:

* `## Output Format` whenever the skill produces an artifact. Name the owning Pydantic model or
  specification; do not restate field lists. Every skill in this repository produces one, so
  `verify_skills` requires the section from all of them and warns when it is absent.

Recommended sections:

* additional phase or protocol sections when the workflow is complex

---

## Writing Rules

Skills must follow `styleguide/agent_instructions_styleguide.md` and
`styleguide/llm_prompting_styleguide.md`. In particular:

* Use imperative mood.
* Use concrete file paths instead of abstract code descriptions.
* Keep trigger wording in `description` specific enough for discovery.
* Keep the body compatible with both Claude Code and Codex; isolate and document any
  runtime-specific behavior.
* Use at most five emphasized `MUST`, `NEVER`, or `CRITICAL` rules per file.
* Provide Do/Don't examples for every non-trivial judgment rule.
* Reference schemas, specifications, and style guides instead of copying their content. A skill
  must not become a second source of truth for any format.
* Delimit source text as untrusted data and instruct the model not to follow instructions found
  inside it.
* Use only synthetic, confidentiality-safe examples. Never include real user text.

---

## Verification Rules

`verify_skills` validates every canonical skill and its wrappers. It reports diagnostics using the
stable codes registered in `src/glite_english_audit/diagnostics/codes.py`. The registry is the
source of truth for code meanings and severities; the table below summarizes the skill-verifier
codes.

### Errors

| Code | Description |
| --- | --- |
| `SKILL_MISSING_FILE` | A canonical skill directory has no `SKILL.md`, or the file is empty |
| `SKILL_FRONTMATTER_INVALID` | Frontmatter is missing, unparsable, or lacks `name` or `description` |
| `SKILL_NAME_MISMATCH` | Frontmatter `name` does not match the skill directory slug |
| `SKILL_VERSION_INVALID` | The body lacks a plain-integer `**Version**` marker |
| `SKILL_TITLE_COUNT` | The body does not contain exactly one top-level title |
| `SKILL_SECTION_MISSING` | A required section (Goal, Inputs, Context, Steps, Done When, Forbidden) is missing |
| `SKILL_EMPHASIS_BUDGET_EXCEEDED` | More than five emphasized MUST, NEVER, or CRITICAL rules in one file |
| `SKILL_WRAPPER_MISSING` | A generated `.claude/skills` or `.codex/skills` wrapper is missing |
| `SKILL_WRAPPER_DRIFT` | A generated wrapper no longer matches its canonical skill |
| `SKILL_REFERENCED_FILE_MISSING` | A local file referenced by a skill does not exist in the repository |

### Warnings

| Code | Description |
| --- | --- |
| `SKILL_OUTPUT_FORMAT_MISSING` | A skill that produces an artifact has no `## Output Format` section |

A changed skill cannot pass CI until `verify_skills` reports no errors and the semantic review
(`check-skill`) succeeds.
