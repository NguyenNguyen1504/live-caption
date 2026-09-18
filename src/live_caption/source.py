"""Interchangeable event sources for the caption application."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

from live_caption.client import (
    ApiResponseError,
    CaptionClientError,
    LocalApiClient,
    StreamStopped,
    validate_status,
)

RECONNECT_MAX_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Notice:
    """One source-to-UI message, tagged to reject retired workers."""

    generation: int
    kind: str
    value: Any = None


class StreamWorker(threading.Thread):
    """Continuously drain the real API without blocking the Tk event loop."""

    def __init__(
        self,
        client: LocalApiClient,
        notices: queue.SimpleQueue[Notice],
        stop_event: threading.Event,
        generation: int,
    ) -> None:
        super().__init__(name="caption-api-stream", daemon=True)
        self.client = client
        self.notices = notices
        self.stop_event = stop_event
        self.generation = generation

    def _put(self, kind: str, value: Any = None) -> None:
        self.notices.put(Notice(self.generation, kind, value))

    def run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                status = self.client.status()
                session_id, partials_available = validate_status(status)
                if not partials_available:
                    self._put("fatal", "Live partial captions are unavailable")
                    return
                self._put("sync", session_id)
                for event in self.client.events(stop_event=self.stop_event):
                    if self.stop_event.is_set():
                        return
                    delay = 1.0
                    self._put("event", event)
            except StreamStopped:
                return
            except ApiResponseError as error:
                if error.status in {400, 401, 403}:
                    self._put("fatal", _friendly_api_error(error))
                    return
                self._put("disconnected", "Caption service unavailable")
            except (CaptionClientError, OSError):
                self._put("disconnected", "Reconnecting to caption service…")
            if self.stop_event.wait(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)


class DemoWorker(threading.Thread):
    """Produce realistic revisions through the exact same event path as the API."""

    SCRIPTS = (
        (
            "Hello everyone",
            "Hello everyone, today we're",
            "Hello everyone, today we're going to talk about",
            "Hello everyone, today we're going to talk about live captions.",
        ),
        (
            "Partial captions can change",
            "Partial captions can change as words are recognized",
            "Partial captions can change as words are recognized in real time",
            "Partial captions can change as words are recognized in real time.",
        ),
    )

    def __init__(
        self,
        notices: queue.SimpleQueue[Notice],
        stop_event: threading.Event,
        generation: int,
        *,
        step_seconds: float = 0.8,
        hold_seconds: float = 2.7,
        repeat: bool = True,
    ) -> None:
        super().__init__(name="caption-demo-stream", daemon=True)
        self.notices = notices
        self.stop_event = stop_event
        self.generation = generation
        self.step_seconds = step_seconds
        self.hold_seconds = hold_seconds
        self.repeat = repeat

    def _put(self, kind: str, value: Any = None) -> None:
        self.notices.put(Notice(self.generation, kind, value))

    def _wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def run(self) -> None:
        cycle = 0
        self._put("sync", "")
        while not self.stop_event.is_set():
            for script_index, script in enumerate(self.SCRIPTS):
                session_id = f"demo-{cycle}-{script_index}"
                self._put("event", _event("session.started", session_id))
                if self._wait(0.35):
                    return
                for revision, text in enumerate(script[:-1], start=1):
                    self._put(
                        "event",
                        _event(
                            "transcript.partial",
                            session_id,
                            segment_id="segment-1",
                            revision=revision,
                            start_ms=0,
                            end_ms=revision * 900,
                            text=text,
                            is_final=False,
                            privacy="unredacted",
                        ),
                    )
                    if self._wait(self.step_seconds):
                        return
                self._put(
                    "event",
                    _event(
                        "transcript.final",
                        session_id,
                        segment_id="final",
                        revision=len(script),
                        start_ms=0,
                        end_ms=len(script) * 900,
                        text=script[-1],
                        is_final=True,
                        privacy="unredacted",
                    ),
                )
                if self._wait(self.step_seconds):
                    return
                self._put("event", _event("session.stopped", session_id))
                if self._wait(self.hold_seconds):
                    return
            cycle += 1
            if not self.repeat:
                return


def _event(name: str, session_id: str, **fields: object) -> dict[str, object]:
    return {"version": 1, "event": name, "session_id": session_id, **fields}


def _friendly_api_error(error: ApiResponseError) -> str:
    if error.reason == "scope_denied":
        return "Caption token lacks the required scopes"
    if error.reason in {"unknown_or_revoked_token", "missing_bearer_token"}:
        return "Caption token is invalid or revoked"
    if error.reason == "too_many_authentication_attempts__try_again_shortly":
        return "Too many authentication attempts; try again later"
    return f"Caption service refused the connection ({error.reason})"
