"""A golden vector for the canonical form Glite's website must reproduce.

`specifications/submission_contract.md` says the canonical payload form is
reproducible byte for byte in TypeScript, because the website recomputes the
hash to verify a manually uploaded package. Every existing hash test round-trips
against the same Python that produced the value, so all of them would still pass
if the canonical form changed — and the website, written against the old form,
would start rejecting every upload.

This pins the actual bytes. If the digest below changes, either the canonical
form changed and every other implementation must change with it, or something
broke. Both are things a maintainer needs told, and neither is visible from a
round-trip.

The payload is synthetic and deliberately awkward in the four ways a second
implementation gets wrong: key order that differs from insertion order, no
whitespace between tokens, non-ASCII left raw rather than escaped, and nested
objects that must sort at every level.
"""

import json

from glite_english_audit.artifacts.hashing import canonical_json_bytes, sha256_hex

GOLDEN_PAYLOAD: dict[str, object] = {
    "submission_schema_version": 1,
    "records": [
        {
            "mistake": "Used 'depends from' instead of 'depends on'.",
            "rule": "The verb depends takes the preposition on.",
            "example": "The result depends on the input.",
            "example_type": "synthetic",
            "source_type": "claude_code",
            "modality": "written",
        }
    ],
    "counts": {"shared_mistakes": 1, "withheld_by_user": 0},
    "note": "naïve café — em—dash and   nbsp",
}

GOLDEN_BYTES = 374
GOLDEN_SHA256 = "0994bc4f6f82ab9fb2617bbdba0ce89c91a6839e0e8c855bce6ea80bb7b0d3c4"


def test_the_canonical_form_still_produces_the_published_digest() -> None:
    encoded = canonical_json_bytes(GOLDEN_PAYLOAD)
    assert len(encoded) == GOLDEN_BYTES
    assert sha256_hex(encoded) == GOLDEN_SHA256


def test_keys_sort_at_every_level() -> None:
    encoded = canonical_json_bytes(GOLDEN_PAYLOAD).decode("utf-8")
    assert encoded.startswith('{"counts":{"shared_mistakes":1,"withheld_by_user":0}')


def test_no_whitespace_separates_tokens() -> None:
    encoded = canonical_json_bytes(GOLDEN_PAYLOAD).decode("utf-8")
    # Spaces inside string values are content and must survive; spaces between
    # tokens are formatting and must not exist.
    assert '", "' not in encoded
    assert '": "' not in encoded


def test_non_ascii_is_kept_raw_rather_than_escaped() -> None:
    # A TypeScript JSON.stringify keeps these raw too, which is why the contract
    # can promise byte equality. Escaping them would silently diverge.
    encoded = canonical_json_bytes(GOLDEN_PAYLOAD).decode("utf-8")
    assert "naïve café" in encoded
    assert "\\u00ef" not in encoded


def test_insertion_order_does_not_change_the_digest() -> None:
    reordered = json.loads(json.dumps(GOLDEN_PAYLOAD))
    shuffled = {key: reordered[key] for key in sorted(reordered, reverse=True)}
    assert sha256_hex(canonical_json_bytes(shuffled)) == GOLDEN_SHA256
