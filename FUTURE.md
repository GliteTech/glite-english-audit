# Future Work

**Status**: Deferred from V1

Features deliberately excluded from V1. Each item needs its own specification, research, and
privacy review before implementation.

## Shared mistake classification

V1 stores plain, taxonomy-free mistake records. It may create temporary plain-English groupings
for one learner's report, but it does not define a generic taxonomy or compare category
frequencies across users.

A later version may add:

- A public, versioned mistake classification.
- A knowledge graph connecting mistakes, plain-English rules, examples, and learning activities.
- Model-assisted mapping of existing taxonomy-free records into the shared classification.
- Human and automated quality checks for classification consistency.
- Migration and reclassification when the shared model changes, without rewriting original
  mistake records.
- Aggregate category frequencies and typical mistake rates by category after enough valid
  anonymous data exists.

This work requires a separate specification and privacy review before implementation.

## Learner comparisons and progress

A later version may add:

- Comparison with aggregated anonymous Glite users after enough validated data exists.
- Typical mistake rates under the future shared classification.
- Optional comparisons by native language, adult age range, country, or learning goal when
  privacy-reviewed cohort sizes are large enough.
- Longitudinal comparison across several audits from one learner.
- Normalization for corpus size, source mix, written versus spoken-ASR modality, coverage, and
  untrusted client-reported denominators.
- Minimum cohort sizes, suppression, differencing protection, and other disclosure controls for
  every comparison.

Future comparisons must not use competitive or shaming rankings.

## Additional sources

Later adapters may include Superwhisper, VoiceInk, VS Code Copilot/Agent, OpenWhispr, Zed,
Windsurf, Warp, Continue, Kilo Code, Kimi CLI, Qwen Code, Factory Droid, and other sources that
pass the full research, provenance, privacy, and cross-platform fixture gates.

## Localization

V1 user-facing text is English-only. A later version may localize onboarding, consent, reports,
downloads, and practice interfaces after a separate terminology, quality, and consent-copy
review.

## Accounts

V1 is anonymous and has no account linking. A later version may add optional account linking
only after a separate identity, consent, deletion, security, and longitudinal-data specification.
