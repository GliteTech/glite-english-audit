"""The single project-wide consent policy version.

Every consent surface — the run manifest's :class:`ConsentState`, the review
page copy, and the submission request envelope — records this version. Any
change to consent wording or privacy behavior bumps it, which invalidates
remembered local-scan consent (specification, 2.2) and appears in checkpoint
compatibility fingerprints.
"""

CONSENT_POLICY_VERSION = "1"
