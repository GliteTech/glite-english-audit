"""Scale gate (specification, 13.8): 1M eligible words, 50k utterances.

Generates a synthetic corpus and runs the deterministic normalization and
counting pipeline. Marked ``slow``: the full gate runs it, but developers can
deselect it with ``-m "not slow"`` during quick iterations.
"""

import resource
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from glite_english_audit.artifacts.enums import Modality, TextStatus
from glite_english_audit.artifacts.models import NormalizedUtterance
from glite_english_audit.normalization.authorship import strip_non_authored
from glite_english_audit.normalization.dedup import dedupe
from glite_english_audit.normalization.language import classify_english
from glite_english_audit.normalization.tokenizer import count_words

_UTTERANCES = 50_000
_WORDS_PER_UTTERANCE = 21  # 50k x 21 > 1M words

_SENTENCES = (
    "Yesterday I tried to explain the new caching idea to my colleague and it went fine",
    "Please check the second draft because I am not sure the wording sounds natural enough",
    "We should probably move the meeting to Thursday since everyone is busy this afternoon",
    "I very like this approach but maybe we can simplify the first part a little",
    "The deployment finished without errors so I will continue with the documentation now",
)


def _synthetic_utterance(index: int) -> NormalizedUtterance:
    base = _SENTENCES[index % len(_SENTENCES)]
    text = f"{base} number {index % 7} and then some more words follow here"
    return NormalizedUtterance(
        utterance_id=f"scale-{index:06d}",
        source_adapter="claude_code",
        adapter_version="1.0.0",
        session_hash=f"{index % 997:064x}"[-64:],
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        text=text,
        modality=Modality.WRITTEN,
        text_status=TextStatus.VERBATIM,
        authorship_confidence=0.9,
        authorship_basis="explicit_user_role",
        source_path_hash="a" * 64,
        destination_app=None,
        content_flags=[],
    )


@pytest.mark.slow
def test_million_word_deterministic_pipeline() -> None:
    start = time.monotonic()
    utterances = [_synthetic_utterance(i) for i in range(_UTTERANCES)]

    eligible = []
    total_words = 0
    for utterance in utterances:
        cleaned = strip_non_authored(utterance.text).cleaned_text.strip()
        decision = classify_english(cleaned)
        assert not decision.quarantined
        assert decision.english_text is not None
        total_words += count_words(decision.english_text)
        eligible.append(utterance)
    assert total_words >= 1_000_000

    outcome = dedupe(eligible)
    # Identical sentence templates repeat far apart in time, so they must
    # survive as genuinely repeated language rather than collapse.
    assert len(outcome.canonical) + len(outcome.excluded) == _UTTERANCES
    assert len(outcome.canonical) >= _UTTERANCES * 0.9

    elapsed = time.monotonic() - start
    assert elapsed < 300, f"deterministic scale run took {elapsed:.0f}s"

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    assert peak_bytes < 1_073_741_824, f"peak RSS {peak_bytes / 1e6:.0f} MB exceeds 1 GiB"
