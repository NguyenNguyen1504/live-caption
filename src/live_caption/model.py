"""Pure caption state derived from version 1 API events.

Keeping this separate from Tk makes the important revision semantics easy to
test: a partial replaces the older revision of the same segment, while a final
transcript supersedes every provisional segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Segment:
    revision: int
    start_ms: int
    text: str


class CaptionState:
    """Apply API events and expose the caption text that should be visible."""

    def __init__(self) -> None:
        self.session_id = ""
        self._segments: dict[str, Segment] = {}
        self._final_text = ""
        self._is_final = False

    @property
    def text(self) -> str:
        if self._final_text:
            return self._final_text
        ordered = sorted(self._segments.items(), key=lambda item: (item[1].start_ms, item[0]))
        return " ".join(segment.text.strip() for _, segment in ordered if segment.text.strip())

    def reset(self, session_id: str = "") -> str:
        self.session_id = session_id
        self._segments.clear()
        self._final_text = ""
        self._is_final = False
        return self.text

    def sync(self, session_id: str) -> str:
        """Reset after connecting because version 1 has no transcript replay."""
        return self.reset(session_id)

    def apply(self, event: Mapping[str, Any]) -> str | None:
        """Apply one event.

        Returns the new display text when the display changed, otherwise
        ``None``. Unknown future version-1 events are intentionally ignored.
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
                # A final transcript supersedes every provisional segment,
                # including a delayed partial delivered out of order.
                return None
            previous = self._segments.get(segment_id)
            if previous is not None and previous.revision >= revision:
                return None
            before = self.text
            self._final_text = ""
            self._segments[segment_id] = Segment(revision, start_ms, text)
            after = self.text
            return after if after != before else None

        if name == "transcript.final":
            text = event.get("text")
            if not isinstance(text, str):
                return None
            before = self.text
            if session_id != self.session_id:
                self.reset(session_id)
            self._segments.clear()
            self._final_text = text.strip()
            self._is_final = True
            after = self.text
            return after if after != before else None

        return None
