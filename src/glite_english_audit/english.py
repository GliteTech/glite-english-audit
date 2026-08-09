"""Agreement helpers for sentences this product builds around a count.

A count interpolated into a sentence decides the noun after it, and often the
verb and the pronoun too. Getting that wrong is a small bug in most software
and a disqualifying one here: this tool corrects a learner's subject-verb
agreement, so a screen reading "Will send 1 mistakes." undoes the authority the
correction depends on. That sentence was real, and it sat on the last screen
before submission.

Six instances of the same defect were found in one pass, in five different
modules, each solving the problem separately or not at all. This module is the
one place, so the seventh instance has somewhere to go instead of becoming a
sixth private helper.

Rules deliberately not generalized: no attempt at irregular plurals beyond an
explicit table, and no article selection. A wrong plural is bad English; an
invented one such as "entrie" or "a hour" is worse, and silently producing it
is exactly the failure this module exists to prevent. A noun this module does
not know is returned unchanged, which is visibly imperfect rather than
confidently wrong.
"""

from collections.abc import Sequence

# Nouns whose plural is not the singular plus "s". Extended deliberately, one
# entry at a time, as user-facing text needs them.
_IRREGULAR: dict[str, str] = {}

# The reverse direction, for call sites that hold a plural. A table rather than
# a rule on purpose: stripping a trailing "s" turns "entries" into "entrie",
# which is worse than leaving the plural alone, and I wrote exactly that bug
# into this module before a test caught it.
_KNOWN_SINGULARS: dict[str, str] = {"sessions": "session", "messages": "message"}


def plural(count: int, singular: str) -> str:
    """``singular`` at a count of one, its plural otherwise.

    A count reaches the user inside a sentence, so the noun after it has to
    agree: one message, not "1 messages".
    """
    if count == 1:
        return singular
    return _IRREGULAR.get(singular, f"{singular}s")


def singularize(count: int, plural_form: str) -> str:
    """The singular of ``plural_form`` at a count of one, else ``plural_form``.

    For call sites that hold the plural, such as a configurable unit noun. A
    form this module does not know is returned unchanged: "0 of 1 entries" is
    wrong, and "0 of 1 entrie" is worse.
    """
    if count != 1:
        return plural_form
    return _KNOWN_SINGULARS.get(plural_form, plural_form)


def verb(count: int, singular_form: str, plural_form: str) -> str:
    """The verb form agreeing with ``count``: ``is``/``are``, ``was``/``were``."""
    return singular_form if count == 1 else plural_form


def and_list(items: Sequence[str]) -> str:
    """Join for running prose: "A", "A and B", "A, B, and C".

    A comma-joined list dropped into a sentence reads as a column of data
    pasted into prose. This project uses the serial comma everywhere else.
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"
