"""Utterances that are a pasted document, decided without an agent.

Step c asks, for every utterance, which spans the learner actually wrote. Most
of that question needs judgment. One large slice of it does not.

People paste documents into a coding agent -- a specification, a plan, a page of
notes -- and the message *is* the document. The learner did not write those words
in that message, so step c keeps nothing from them, and it does so with a
regularity a model is not needed to reproduce.

**Measured on run-4806c5a4629b4652b072b65e99ff9858**, over all 6,725 utterances
that have both a projection and a decision on disk:

    utterances matching this rule         2,705  (40.2% of all)
    of those, the agent kept nothing      2,692  (99.52%)
    of those, the agent kept something       13  (0.48%)

    sessions needing no agent at all      2,691 of 3,178  (84.7%)

Two corrections are recorded here because both were nearly shipped.

**A 402-session sample said 0.4 lines and 1.6% error. The full 3,178 sessions
said 2.88%** for the same rule -- the sample was optimistic by nearly a factor of
two. Measure the corpus, not a sample of it.

**The rule has an upper bound, which is the opposite of the obvious.** Structured
messages of 150 lines or more were, without exception, the learner's OWN writing:
38 matched at that length and the agent kept something from all 38. People paste
documents of forty to a hundred lines; past that, they are writing the document
themselves. Dropping the ceiling took the error rate from 1.86% to 0.48% and cost
one session of coverage.

**What this is not.** The mirror rule -- short plain prose is kept verbatim -- was
measured and fails: of 184 single-line non-markdown utterances only 97 came back
identical, 77 were trimmed and 10 emptied. Deciding those means judging authorship
inside one line, which is what step c is for. Nothing here tries to.
"""

import re

__all__ = ["MAXIMUM_LINES", "MINIMUM_LINES", "is_pasted_document"]

MINIMUM_LINES = 80
"""Below this, a structured message is left to the agent.

Someone writing three bullets in a sentence is writing. Measured error at this
threshold is 0.48%; at 60 lines it is 0.88% and at 40 lines 1.54%, for the same
coverage in whole sessions -- because the messages this is aimed at are whole
documents, and lowering the floor admits mixed messages without admitting more
pastes.
"""

MAXIMUM_LINES = 150
"""At or above this, a structured message is the learner's own document.

Not a safety margin -- a measured inversion. All 38 matches of 150 lines or more
had text the agent kept. Past about a hundred lines a structured message stops
being something someone pasted and starts being something they wrote.
"""

_STRUCTURE = re.compile(
    r"""^(
        \#{1,6}\ |      # a markdown heading
        \s*[-*]\s |     # a bullet
        \s*\d+\.\s      # a numbered item
    )""",
    re.MULTILINE | re.VERBOSE,
)
"""Markdown structure at the start of a line.

Deliberately the three cheapest markers rather than a markdown parser. Each was
measured separately and they agree almost exactly, because they co-occur in the
same pasted documents. A parser would find the same messages and add a dependency
to a project whose entire dependency list is two libraries.
"""


def is_pasted_document(text: str) -> bool:
    """True when an utterance is a document the learner pasted, not wrote.

    Structure alone is not enough and length alone is not enough: long prose can
    be someone's own, and a short list can be quoted. Both together, within the
    measured band, are what identifies a paste.
    """
    lines = text.count("\n") + 1
    if not MINIMUM_LINES <= lines < MAXIMUM_LINES:
        return False
    return _STRUCTURE.search(text) is not None
