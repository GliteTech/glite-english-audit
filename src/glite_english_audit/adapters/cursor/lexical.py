"""Lexical editor-state projection and the per-bubble rawness gate.

Implements sections 5.2, 5.4, and 5.5 of ``specifications/sources/cursor.md``.
A Cursor user bubble stores both ``text`` (what was sent) and ``richText`` (the
serialized Lexical state of the prompt box at send time). Evidence E11 proved
that, for the tested variant, the first is a faithful serialization of the
second, so a bubble carries analyzable text only when the two reconcile.

The projection rule is load-bearing, not a detail: an earlier reader that
dropped paragraph boundaries manufactured 51.8% false divergence (E11). Root
children are paragraphs joined with newlines; inside a paragraph ``text`` nodes
concatenate, ``tab`` and ``linebreak`` render literally, and ``mention`` nodes
render as their display name.

Nothing here touches the filesystem or a database; every function is pure so
discovery, extraction, and verification can re-run the identical gate.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

# Node types in the E11 census (spec 5.2). Any other type is rendered by
# recursing into its children and flagged; the adapter never guesses at what an
# unknown node contributes (spec 9).
KNOWN_NODE_TYPES: frozenset[str] = frozenset(
    {"root", "paragraph", "text", "mention", "tab", "linebreak"}
)

# E11 recorded the mention node type but not which key holds the display name,
# so the first present key wins. A wrong choice is visible rather than silent:
# the bubble then fails to reconcile and contributes no text at all.
MENTION_NAME_KEYS: tuple[str, ...] = ("mentionName", "name", "label", "text")

_HORIZONTAL_RUN = re.compile(r"[ \t]{2,}")


class TextGate(StrEnum):
    """Outcome of the spec 6.3 rawness gate for one user bubble."""

    VERBATIM_EXACT = "verbatim_exact"
    VERBATIM_WHITESPACE = "verbatim_whitespace"
    VERBATIM_MENTION_SIGIL = "verbatim_mention_sigil"
    NO_EDITOR_STATE = "no_editor_state"
    PROJECTION_MISMATCH = "text_projection_mismatch"


VERBATIM_GATES: frozenset[TextGate] = frozenset(
    {TextGate.VERBATIM_EXACT, TextGate.VERBATIM_WHITESPACE, TextGate.VERBATIM_MENTION_SIGIL}
)


@dataclass(frozen=True)
class LexicalProjection:
    """Plain-text projection of one composer editor state (spec 5.2)."""

    text: str
    # Display names in document order, duplicates kept: stripping removes at
    # most as many bare occurrences as there were mention nodes.
    mention_names: tuple[str, ...]
    unknown_node_types: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    """The gate verdict plus the text the bubble may contribute."""

    gate: TextGate
    text: str
    mention_stripped: bool
    unknown_nodes: bool

    @property
    def verbatim(self) -> bool:
        return self.gate in VERBATIM_GATES


@dataclass
class _RenderState:
    mention_names: list[str] = field(default_factory=list)
    unknown_types: set[str] = field(default_factory=set)


def normalize_whitespace(value: str) -> str:
    """Whitespace normalization used by the spec 5.4 second comparison."""
    return " ".join(value.split())


def _mention_name(node: dict[str, object]) -> str:
    for key in MENTION_NAME_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _render_inline(node: object, parts: list[str], state: _RenderState) -> None:
    """Render one node into a paragraph's text parts."""
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type == "text":
        text = node.get("text")
        if isinstance(text, str):
            parts.append(text)
        return
    if node_type == "tab":
        parts.append("\t")
        return
    if node_type == "linebreak":
        parts.append("\n")
        return
    if node_type == "mention":
        name = _mention_name(node)
        if name:
            state.mention_names.append(name)
            parts.append(name)
        return
    if not isinstance(node_type, str) or node_type not in KNOWN_NODE_TYPES:
        state.unknown_types.add(node_type if isinstance(node_type, str) else "<untyped>")
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            _render_inline(child, parts, state)


def project_editor_state(raw: object) -> LexicalProjection | None:
    """Project a serialized Lexical editor state to plain text (spec 5.2).

    Returns ``None`` when no usable editor state is present, which is the
    spec 6.3.2 ``no_editor_state`` bucket: absent, empty, non-JSON, or
    structurally unusable ``richText``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    root = payload.get("root")
    if not isinstance(root, dict):
        return None
    children = root.get("children")
    if not isinstance(children, list) or not children:
        return None
    state = _RenderState()
    root_type = root.get("type")
    if root_type is not None and root_type != "root":
        state.unknown_types.add(root_type if isinstance(root_type, str) else "<untyped>")
    blocks: list[str] = []
    for child in children:
        parts: list[str] = []
        _render_inline(child, parts, state)
        blocks.append("".join(parts))
    return LexicalProjection(
        text="\n".join(blocks),
        mention_names=tuple(state.mention_names),
        unknown_node_types=tuple(sorted(state.unknown_types)),
    )


def _unique_longest_first(names: tuple[str, ...]) -> list[str]:
    return sorted(set(names), key=lambda name: (-len(name), name))


def remove_mention_sigils(text: str, mention_names: tuple[str, ...]) -> str:
    """Drop the ``@`` the stored text keeps on mention tokens (spec 5.3).

    The projection renders a mention as its display name; the stored text keeps
    the sigil in front of it. That one character explained the whole 15.2%
    residual bucket, so it is accounted for before a bubble is called divergent.
    """
    for name in _unique_longest_first(mention_names):
        text = text.replace(f"@{name}", name)
    return text


def strip_mentions(text: str, mention_names: tuple[str, ...]) -> tuple[str, bool]:
    """Remove ``@name`` mention tokens from text to analyze (spec 5.5).

    Mention tokens come from a file picker, not from composing prose, so they
    are removed and never counted. Sigil-prefixed occurrences are all removed;
    bare occurrences are removed at most once per mention node so a display
    name that is also an ordinary word cannot erase authored text.
    """
    if not mention_names:
        return text, False
    counts = Counter(mention_names)
    removed = False
    for name in _unique_longest_first(mention_names):
        budget = counts[name]
        sigil = f"@{name}"
        sigil_hits = text.count(sigil)
        if sigil_hits:
            text = text.replace(sigil, "")
            removed = True
            budget -= sigil_hits
        while budget > 0 and name in text:
            text = text.replace(name, "", 1)
            removed = True
            budget -= 1
    if not removed:
        return text, False
    collapsed = _HORIZONTAL_RUN.sub(" ", text)
    return "\n".join(line.strip() for line in collapsed.split("\n")).strip(), True


def reconcile(text: str, rich_text: object) -> GateResult:
    """Run the spec 6.3 gate on one user bubble.

    ``text`` is the stored prompt; ``rich_text`` its sibling ``richText`` field.
    The returned text is the stored prompt with mention tokens stripped, and is
    empty for every non-reconciling bubble: those contribute inventory counts
    only.
    """
    projection = project_editor_state(rich_text)
    if projection is None:
        return GateResult(
            gate=TextGate.NO_EDITOR_STATE, text="", mention_stripped=False, unknown_nodes=False
        )
    unknown_nodes = bool(projection.unknown_node_types)
    if text == projection.text:
        gate = TextGate.VERBATIM_EXACT
    elif normalize_whitespace(text) == normalize_whitespace(projection.text):
        gate = TextGate.VERBATIM_WHITESPACE
    else:
        desigiled = remove_mention_sigils(text, projection.mention_names)
        if desigiled == projection.text or normalize_whitespace(desigiled) == normalize_whitespace(
            projection.text
        ):
            gate = TextGate.VERBATIM_MENTION_SIGIL
        else:
            return GateResult(
                gate=TextGate.PROJECTION_MISMATCH,
                text="",
                mention_stripped=False,
                unknown_nodes=unknown_nodes,
            )
    stripped, mention_stripped = strip_mentions(text, projection.mention_names)
    return GateResult(
        gate=gate,
        text=stripped,
        mention_stripped=mention_stripped,
        unknown_nodes=unknown_nodes,
    )
