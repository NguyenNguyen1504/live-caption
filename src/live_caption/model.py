"""Pure, revision-aware caption state derived from version 1 API events.

The API's segment is the recognizer's mutable unit and can grow for a long
time. It is deliberately not also the caption's mutable unit. ``CaptionState``
keeps the recognizer input continuous, but freezes short prefix chunks as soon
as a boundary is already present in a partial. Only the remaining suffix is
then replaced by later revisions.

No timer or buffering is involved here: every partial is applied
synchronously when it arrives. A final transcript still supersedes all
provisional text, as required by the version 1 API contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# These are caption-display boundaries only. They never restart the ASR or
# delay an incoming partial.
SOFT_SEGMENT_WORDS = 10
HARD_SEGMENT_WORDS = 16
MIN_CLAUSE_WORDS = 6
SOFT_BOUNDARY_SLOP = 2
ANCHOR_SEARCH_RADIUS = 32

_CLOSING_PUNCTUATION = "\"'\N{RIGHT SINGLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK})]}"
_SENTENCE_ENDINGS = (".", "!", "?", "\N{HORIZONTAL ELLIPSIS}")
_CLAUSE_ENDINGS = (",", ";", ":", "\N{EM DASH}")


@dataclass(slots=True)
class _SourceSegment:
    """One upstream ASR segment and its downstream caption boundaries."""

    revision: int
    start_ms: int
    words: tuple[str, ...]
    active_start: int = 0
    committed: list[str] = field(default_factory=list)
    closed: bool = False

    @property
    def active_words(self) -> tuple[str, ...]:
        if self.closed:
            return ()
        return self.words[self.active_start :]

    @property
    def active_text(self) -> str:
        return " ".join(self.active_words)

    def rendered_parts(self) -> list[str]:
        parts = list(self.committed)
        active = self.active_text
        if active:
            parts.append(active)
        return parts

    def replace(self, revision: int, words: tuple[str, ...]) -> None:
        """Replace only the mutable suffix with a newer ASR hypothesis."""
        self.active_start = _map_word_boundary(self.words, words, self.active_start)
        self.words = words
        self.revision = revision

    def commit_available_boundaries(self) -> None:
        """Freeze all boundaries already justified by the current partial."""
        while True:
            active = self.active_words
            boundary = _next_boundary(active)
            if boundary is None:
                return
            text = " ".join(active[:boundary]).strip()
            if text:
                self.committed.append(text)
            self.active_start += boundary

    def close(self) -> None:
        """Commit the remainder when the ASR starts a later source segment."""
        if self.closed:
            return
        active = self.active_text
        if active:
            self.committed.append(active)
        self.active_start = len(self.words)
        self.closed = True


class CaptionState:
    """Apply API events and expose short, stable caption segments."""

    def __init__(self) -> None:
        self.session_id = ""
        self._segments: dict[str, _SourceSegment] = {}
        self._active_segment_id: str | None = None
        self._final_text = ""
        self._is_final = False

    @property
    def text(self) -> str:
        if self._final_text:
            return self._final_text
        return " ".join(self.caption_segments)

    @property
    def caption_segments(self) -> tuple[str, ...]:
        """Committed chunks followed by the one currently mutable suffix."""
        parts: list[str] = []
        for _, segment in self._ordered_segments():
            parts.extend(segment.rendered_parts())
        return tuple(part for part in parts if part)

    @property
    def committed_segments(self) -> tuple[str, ...]:
        parts: list[str] = []
        for _, segment in self._ordered_segments():
            parts.extend(segment.committed)
        return tuple(parts)

    @property
    def active_text(self) -> str:
        if self._active_segment_id is None:
            return ""
        segment = self._segments.get(self._active_segment_id)
        return segment.active_text if segment is not None else ""

    def _ordered_segments(self) -> list[tuple[str, _SourceSegment]]:
        return sorted(
            self._segments.items(), key=lambda item: (item[1].start_ms, item[0])
        )

    def reset(self, session_id: str = "") -> str:
        self.session_id = session_id
        self._segments.clear()
        self._active_segment_id = None
        self._final_text = ""
        self._is_final = False
        return self.text

    def sync(self, session_id: str) -> str:
        """Reset after connecting because version 1 has no transcript replay."""
        return self.reset(session_id)

    def apply(self, event: Mapping[str, Any]) -> str | None:
        """Apply one event and immediately return changed display text.

        There is intentionally no clock, debounce, punctuation wait, or
        minimum update interval in this path. Unknown future version-1 events
        are ignored.
        """
        if event.get("version") != 1:
            return None
        name = event.get("event")
        session_id = event.get("session_id")
        if not isinstance(session_id, str):
            return None

        if name == "session.started":
            return self.reset(session_id)

        if name == "session.cancelled":
            if session_id == self.session_id:
                return self.reset()
            return None

        if name == "transcript.partial":
            return self._apply_partial(event, session_id)

        if name == "transcript.final":
            text = event.get("text")
            if not isinstance(text, str):
                return None
            before = self.text
            if session_id != self.session_id:
                self.reset(session_id)
            self._segments.clear()
            self._active_segment_id = None
            self._final_text = text.strip()
            self._is_final = True
            after = self.text
            return after if after != before else None

        return None

    def _apply_partial(
        self, event: Mapping[str, Any], session_id: str
    ) -> str | None:
        segment_id = event.get("segment_id")
        revision = event.get("revision")
        start_ms = event.get("start_ms", 0)
        text = event.get("text")
        if (
            not isinstance(segment_id, str)
            or not isinstance(revision, int)
            or not isinstance(start_ms, int)
            or not isinstance(text, str)
        ):
            return None
        if session_id != self.session_id:
            self.reset(session_id)
        elif self._is_final:
            # The canonical final wins over delayed, out-of-order partials.
            return None

        words = tuple(text.split())
        previous = self._segments.get(segment_id)
        if previous is not None and previous.revision >= revision:
            return None
        if previous is not None and previous.closed:
            # Starting a later upstream segment is an explicit boundary already
            # supplied by the recognizer. Do not let a late revision reopen it.
            return None

        before = self.text
        self._final_text = ""

        if previous is None:
            previous = _SourceSegment(revision, start_ms, words)
            self._segments[segment_id] = previous
            self._activate_if_latest(segment_id, previous)
        else:
            previous.replace(revision, words)

        previous.commit_available_boundaries()
        after = self.text
        return after if after != before else None

    def _activate_if_latest(self, segment_id: str, segment: _SourceSegment) -> None:
        active = (
            self._segments.get(self._active_segment_id)
            if self._active_segment_id is not None
            else None
        )
        if active is None:
            self._active_segment_id = segment_id
            return
        if (segment.start_ms, segment_id) > (
            active.start_ms,
            self._active_segment_id or "",
        ):
            active.close()
            self._active_segment_id = segment_id
        else:
            # An older source segment arriving out of order must not displace
            # the current mutable suffix.
            segment.commit_available_boundaries()
            segment.close()


def _next_boundary(words: Sequence[str]) -> int | None:
    """Choose a boundary using only words already present in this partial."""
    if not words:
        return None

    # A completed sentence is always natural enough to commit immediately;
    # doing so does not wait for or inspect a future word.
    for index, word in enumerate(words[:HARD_SEGMENT_WORDS], start=1):
        if _has_ending(word, _SENTENCE_ENDINGS):
            return index

    # Once near the soft target, use a clause boundary that is already known.
    lower = max(MIN_CLAUSE_WORDS, SOFT_SEGMENT_WORDS - SOFT_BOUNDARY_SLOP)
    clause_boundaries = [
        index
        for index, word in enumerate(words, start=1)
        if lower <= index <= HARD_SEGMENT_WORDS
        and _has_ending(word, _CLAUSE_ENDINGS)
    ]
    if len(words) >= SOFT_SEGMENT_WORDS and clause_boundaries:
        return min(
            clause_boundaries,
            key=lambda index: (abs(index - SOFT_SEGMENT_WORDS), index),
        )

    if len(words) >= HARD_SEGMENT_WORDS:
        # No useful punctuation was available. Freeze a soft-sized prefix so
        # the remainder has room to grow instead of emitting hard-sized chunks
        # mechanically forever.
        return SOFT_SEGMENT_WORDS
    return None


def _has_ending(word: str, endings: tuple[str, ...]) -> bool:
    return word.rstrip(_CLOSING_PUNCTUATION).endswith(endings)


def _map_word_boundary(
    old_words: Sequence[str], new_words: Sequence[str], boundary: int
) -> int:
    """Map a cursor between old words into a revised word sequence.

    Small anchors on either side of the cursor keep this linear in transcript
    length (with a small constant), rather than running an unbounded diff on
    the UI update path. Insertions at the exact boundary remain mutable; edits
    wholly before it merely shift the cursor.
    """
    boundary = max(0, min(boundary, len(old_words)))
    if boundary == 0:
        return 0

    # Prefer an anchor in the mutable suffix. Trying a few offsets tolerates an
    # autocorrection of the first active word without making older text mutable.
    active = old_words[boundary:]
    for offset in range(min(5, len(active))):
        anchor = active[offset : offset + min(3, len(active) - offset)]
        if len(anchor) < 2:
            continue
        found = _nearest_subsequence(new_words, anchor, boundary + offset)
        if found is not None:
            return max(0, found - offset)

    # If all of the active prefix changed (or it was empty), anchor on the end
    # of the committed source prefix. This also accounts for insertions and
    # deletions made by the ASR before the local caption boundary.
    for width in range(min(4, boundary), 0, -1):
        anchor = old_words[boundary - width : boundary]
        found = _nearest_subsequence(new_words, anchor, boundary - width)
        if found is not None:
            return min(len(new_words), found + width)

    # An arbitrary rewrite spanning both sides has no reliable textual anchor.
    # Keep the old cursor position as the least surprising bounded fallback.
    return min(boundary, len(new_words))


def _nearest_subsequence(
    words: Sequence[str], anchor: Sequence[str], expected: int
) -> int | None:
    if not anchor or len(anchor) > len(words):
        return None
    last_start = len(words) - len(anchor)
    expected = max(0, min(expected, last_start))

    # The normal ASR case is an append or a small correction near the active
    # boundary. Check that bounded neighborhood first so update cost does not
    # grow with a long recognizer segment.
    checked: set[int] = set()
    for distance in range(ANCHOR_SEARCH_RADIUS + 1):
        for index in (expected - distance, expected + distance):
            if index < 0 or index > last_start or index in checked:
                continue
            checked.add(index)
            if words[index : index + len(anchor)] == anchor:
                return index

    # A large insertion/deletion before the boundary is unusual, but a linear
    # fallback still preserves alignment without an unbounded quadratic diff.
    for index in range(last_start + 1):
        if index not in checked and words[index : index + len(anchor)] == anchor:
            return index
    return None
