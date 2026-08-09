"""The first-run sentence names the apps, and its count stays true.

Before the first scan the user is told what will be read. "This computer" is a
scope nobody can picture, so the sentence names four apps they will recognise
and counts the rest. The count is the part that rots: adding a tenth adapter
makes "five other" a false statement in the one sentence the user is asked to
agree to, and nothing else in the suite would notice.

So the sentence is pinned to the registry rather than to a number written down
twice. Adding an adapter fails this file until the sentence moves with it.
"""

import re

from glite_english_audit.adapters import register_all
from glite_english_audit.discovery.registry import adapter_ids
from glite_english_audit.paths import repo_root

_SKILL = repo_root() / "skills" / "run-english-audit" / "SKILL.md"

# The spoken sentence, not the prose around it: the block the skill marks `Do:`
# is what reaches the user, and it is the only text this file governs.
_DO_BLOCK = re.compile(
    r"3\. First-run explanation.*?Do:\s*```text\n(.*?)```",
    re.DOTALL,
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _sentence() -> str:
    match = _DO_BLOCK.search(_SKILL.read_text(encoding="utf-8"))
    assert match is not None, "the first-run Do: block moved or lost its text fence"
    return " ".join(match.group(1).split())


def _registered() -> set[str]:
    register_all()
    return set(adapter_ids())


def test_every_app_named_to_the_user_is_an_adapter_that_exists() -> None:
    """A name in the sentence promises a scan. Nothing may be named that is not scanned."""
    named = re.findall(r"(?:[A-Z][a-z]+ )*(?:Claude Code|Codex|Cursor|Wispr Flow)", _sentence())
    assert named, "the sentence names no apps at all"
    ids = {name.strip().lower().replace(" ", "_") for name in named}
    assert ids <= _registered(), f"named but not registered: {sorted(ids - _registered())}"


def test_the_count_of_unnamed_apps_matches_the_registry() -> None:
    """Four named plus "five other" must equal every adapter the scan looks for.

    Installed or not is irrelevant here: the scan looks for all of them, so the
    consent covers all of them.
    """
    sentence = _sentence()
    named = re.findall(r"(?:Claude Code|Codex|Cursor|Wispr Flow)", sentence)

    match = re.search(r"\b(\w+) other\b", sentence)
    assert match is not None, 'the sentence no longer says "<number> other"'
    word = match.group(1).lower()
    assert word in _NUMBER_WORDS, f"unrecognised number word: {word!r}"

    claimed = len(set(named)) + _NUMBER_WORDS[word]
    actual = len(_registered())
    assert claimed == actual, (
        f"the first-run sentence accounts for {claimed} apps, the registry has {actual}. "
        "Adding an adapter widens what the scan reads, so the sentence the user "
        "agrees to has to say so."
    )


def test_the_sentence_still_says_what_comes_back() -> None:
    """Naming the apps must not crowd out the fact the user cannot infer.

    The apps are the scope; counts-not-messages is the limit. Losing the second
    to make room for the first would trade the more important half away.
    """
    sentence = _sentence().lower()
    assert "counts and dates" in sentence
    assert "never your messages" in sentence
