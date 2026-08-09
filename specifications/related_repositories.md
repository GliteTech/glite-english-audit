# Related Glite repositories and development precedents

This project was designed with several existing Glite repositories as researched precedents. They
are development references, not dependencies: the released `glite-english-audit` tree must remain
understandable, buildable, testable, and usable by someone who has access only to this repository.
A link to another Glite repository is provenance, never the only place a required rule lives.

## Reuse policy

- Before adapting a pattern, a research note records the exact repository, file, commit, applicable
  rule, observed limitations, and license status. Research notes live in the private, Git-ignored
  `temp/findings/` tree during development.
- The adopted rule is then expressed in this repository's own code, specifications, style guides,
  or tests.
- Code or substantial text is copied only after a license and attribution check. Everything else is
  an independent application of the underlying design principle.
- No Glite repository becomes a build, test, runtime, or documentation dependency of this project.

## Adopted precedents

### glite-arf (Apache-2.0)

Adopted: Python 3.12 + uv + strict mypy + Ruff + pytest + pre-commit infrastructure; shared
Codex/Claude Code skill conventions; the Goal/Context/Steps/Done When prompt structure; stable
diagnostic codes; deterministic verifiers. `styleguide/python_styleguide.md`,
`styleguide/agent_instructions_styleguide.md`, and `specifications/agent_skills_specification.md`
are adaptations of its documents, with attribution in `NOTICE`.
Rejected: the ARF runtime and its research-task artifact model; filesystem symlinks for skill
discovery (this project generates Windows-safe wrapper files instead).

### glite-documentation

Adopted: current Glite terminology, source-first documentation discipline, and the separation of
code-verified facts from planning context.
Rejected: reuse of any existing privacy notice. This product requires its own data-flow review,
DPIA decision, privacy notice, and legal review before collection launches.

### glite (monorepo)

Adopted: human-authored guidance as the source for generated agent instructions; gold-set and
held-out evaluation; structured verification results.
Rejected: any dependency on the monorepo; any reuse of production learner data or private fixtures.

### glite-dictionary-pipeline

Adopted: typed Pydantic serialization boundaries, per-item caches with atomic writes,
prompt/model/code-version cache invalidation, bounded validation retries, separation of I/O, pure
calculation, and orchestration, and actual token/cost accounting.
Rejected: immutable-only assets, supersession chains, and JSONL metadata headers. This project uses
replaceable derived artifacts: the run manifest points to one current output per step, and
replacement invalidates downstream outputs.

### glite-analytics

Adopted: adapter isolation, explicit source grain and provenance, incremental date-window
processing, independent source failures, and clear raw/normalized/report boundaries.
Rejected: its extraction code and its weaker privacy assumptions, which target external business
systems, not personal local transcripts.

### glite-webfunnels

Adopted: the Pydantic record/specification/verifier pattern, evidence-resolving diagnostics, and
regenerated-not-hand-edited derived HTML, applied to the local review page.
Rejected: its immutable archive, browser-capture logic, business data, and site design. The review
page here is an interactive loopback application with its own threat model.

## Reviewed but not normative

`glite-active-lessons`, `glite-browser-extension`, `glite-media-verifier`,
`glite-monthly-analytics`, `glite-viral-tests-framework`, `glite-creative-image-generator`, and
assorted worktrees and versioned copies were reviewed and deliberately not adopted as precedents.
Current product, design, and privacy decisions are located through `glite-documentation` and
verified against the current `glite` implementation, never through historical copies.
