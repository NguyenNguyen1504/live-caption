from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from live_caption.client import (
    CaptionClientError,
    Endpoint,
    LocalApiClient,
    WEBSOCKET_GUID,
    validate_status,
)
from live_caption.credentials import CaptionCredentials
from live_caption.model import (
    HARD_SEGMENT_WORDS,
    SOFT_SEGMENT_WORDS,
    CaptionState,
)
from live_caption.overlay import fit_recent_lines
from live_caption.source import DemoWorker


def partial(
    text: str,
    *,
    segment: str = "segment-1",
    revision: int = 1,
    start_ms: int = 0,
    session: str = "session-1",
) -> dict[str, object]:
    return {
        "version": 1,
        "event": "transcript.partial",
        "session_id": session,
        "segment_id": segment,
        "revision": revision,
        "start_ms": start_ms,
        "text": text,
    }


def test_partial_revision_replaces_instead_of_appending() -> None:
    state = CaptionState()
    assert state.apply(partial("Hello everyone")) == "Hello everyone"
    assert state.apply(partial("Hello everyone, today we're", revision=2)) == (
        "Hello everyone, today we're"
    )
    assert state.apply(partial("stale", revision=1)) is None
    assert state.text == "Hello everyone, today we're"


def test_first_and_every_rapid_partial_are_visible_immediately() -> None:
    state = CaptionState()
    words = [f"word-{index}" for index in range(1, HARD_SEGMENT_WORDS)]

    for revision in range(1, len(words) + 1):
        expected = " ".join(words[:revision])
        assert state.apply(partial(expected, revision=revision)) == expected
        assert state.text == expected

    # Nothing in the model waits for punctuation, a timer, another word, or a
    # final result before exposing a partial.
    assert state.committed_segments == ()
    assert state.active_text == " ".join(words)


def test_short_sentence_punctuation_commits_without_waiting_for_more_words() -> None:
    state = CaptionState()
    assert state.apply(partial("This is ready.")) == "This is ready."
    assert state.committed_segments == ("This is ready.",)
    assert state.active_text == ""


def test_punctuation_commits_sentence_and_leaves_following_words_mutable() -> None:
    state = CaptionState()
    state.apply(partial("The first thought ends here. another idea", revision=1))

    assert state.committed_segments == ("The first thought ends here.",)
    assert state.active_text == "another idea"
    assert state.apply(
        partial(
            "The first corrected thought ends here. a better idea",
            revision=2,
        )
    ) == "The first thought ends here. a better idea"
    assert state.committed_segments == ("The first thought ends here.",)
    assert state.active_text == "a better idea"


def test_clause_boundary_near_soft_target_is_preferred() -> None:
    state = CaptionState()
    words = "one two three four five six seven eight nine ten, eleven twelve".split()
    state.apply(partial(" ".join(words)))

    assert state.committed_segments == (" ".join(words[:SOFT_SEGMENT_WORDS]),)
    assert state.active_text == "eleven twelve"


def test_continuous_speech_uses_soft_chunks_at_hard_mutable_limit() -> None:
    state = CaptionState()
    words = [f"word-{index}" for index in range(1, 38)]
    state.apply(partial(" ".join(words)))

    assert [len(chunk.split()) for chunk in state.committed_segments] == [10, 10, 10]
    assert state.active_text == " ".join(words[30:])
    assert len(state.active_text.split()) < HARD_SEGMENT_WORDS
    assert state.text.split() == words


def test_late_punctuation_in_a_large_partial_does_not_bypass_hard_limit() -> None:
    state = CaptionState()
    words = [f"word-{index}" for index in range(1, 24)] + ["finished."]
    state.apply(partial(" ".join(words)))

    assert state.text.split() == words
    assert all(
        len(chunk.split()) <= HARD_SEGMENT_WORDS
        for chunk in state.committed_segments
    )
    assert state.active_text == ""


def test_active_autocorrection_does_not_rewrite_committed_prefix() -> None:
    state = CaptionState()
    original = [f"word-{index}" for index in range(1, 19)]
    state.apply(partial(" ".join(original), revision=1))
    frozen = " ".join(original[:SOFT_SEGMENT_WORDS])

    revised = original.copy()
    revised[2] = "old-prefix-correction"
    revised[12:15] = ["new", "active", "wording"]
    state.apply(partial(" ".join(revised), revision=2))

    assert state.committed_segments == (frozen,)
    assert state.active_text == " ".join(revised[SOFT_SEGMENT_WORDS:])
    assert "old-prefix-correction" not in state.text
    assert "new active wording" in state.text


def test_insertions_and_deletions_before_boundary_do_not_duplicate_suffix() -> None:
    state = CaptionState()
    original = [f"word-{index}" for index in range(1, 19)]
    state.apply(partial(" ".join(original), revision=1))

    inserted = original[:2] + ["inserted"] + original[2:] + ["word-19"]
    state.apply(partial(" ".join(inserted), revision=2))
    assert state.text.split() == original + ["word-19"]

    deleted = original[:4] + original[5:] + ["word-19", "word-20"]
    state.apply(partial(" ".join(deleted), revision=3))
    assert state.text.split() == original[:SOFT_SEGMENT_WORDS] + deleted[9:]
    assert len(state.text.split()) == len(set(state.text.split()))


def test_new_upstream_segment_commits_previous_remainder() -> None:
    state = CaptionState()
    state.apply(partial("a short unfinished phrase", revision=1))
    state.apply(
        partial("the next phrase", segment="segment-2", start_ms=1000, revision=1)
    )

    assert state.committed_segments == ("a short unfinished phrase",)
    assert state.active_text == "the next phrase"
    assert state.apply(partial("late rewrite", revision=2)) is None
    assert state.text == "a short unfinished phrase the next phrase"


def test_segments_are_ordered_by_start_time_even_when_received_out_of_order() -> None:
    state = CaptionState()
    state.apply(partial("later", segment="b", start_ms=1000))
    assert state.apply(partial("first", segment="a", start_ms=0)) == "first later"


def test_final_supersedes_all_partial_segments() -> None:
    state = CaptionState()
    state.apply(partial("provisional"))
    assert state.apply(
        {
            "version": 1,
            "event": "transcript.final",
            "session_id": "session-1",
            "text": "Canonical transcript.",
        }
    ) == "Canonical transcript."
    assert state.text == "Canonical transcript."
    assert state.apply(partial("late partial", revision=99)) is None
    assert state.text == "Canonical transcript."


def test_new_and_cancelled_sessions_clear_provisional_text() -> None:
    state = CaptionState()
    state.apply(partial("old"))
    assert state.apply(
        {"version": 1, "event": "session.started", "session_id": "session-2"}
    ) == ""
    state.apply(partial("new", session="session-2"))
    assert state.apply(
        {"version": 1, "event": "session.cancelled", "session_id": "session-2"}
    ) == ""


def test_endpoint_accepts_only_loopback_without_credentials_or_path() -> None:
    assert Endpoint.parse("http://127.0.0.1:9000").authority == "127.0.0.1:9000"
    assert Endpoint.parse("ws://[::1]:8765").authority == "[::1]:8765"
    with pytest.raises(ValueError, match="loopback"):
        Endpoint.parse("http://example.com:8765")
    with pytest.raises(ValueError, match="credentials"):
        Endpoint.parse("http://user:secret@127.0.0.1:8765")
    with pytest.raises(ValueError, match="path"):
        Endpoint.parse("http://127.0.0.1:8765/v1")


def test_status_requires_live_caption_scopes() -> None:
    base = {"version": 1, "state": {"session_id": "s1", "partials_available": True}}
    with pytest.raises(CaptionClientError, match="transcript:live"):
        validate_status({**base, "capabilities": ["status:read"]})
    assert validate_status(
        {**base, "capabilities": ["status:read", "transcript:live"]}
    ) == ("s1", True)


def test_recent_line_fitting_keeps_the_latest_lines() -> None:
    # A monospace-like measure makes the wrapping deterministic.
    result = fit_recent_lines(
        "one two three four five six", lambda value: len(value), 9, max_lines=2
    )
    assert result == "four five\nsix"


def test_demo_mode_uses_the_same_revision_aware_caption_state() -> None:
    notices = queue.SimpleQueue()
    stop_event = threading.Event()
    worker = DemoWorker(
        notices,
        stop_event,
        generation=7,
        step_seconds=0.001,
        hold_seconds=0.001,
        repeat=False,
    )
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    state = CaptionState()
    displayed: list[str] = []
    final_events = 0
    while not notices.empty():
        notice = notices.get_nowait()
        assert notice.generation == 7
        if notice.kind != "event":
            continue
        changed = state.apply(notice.value)
        if changed:
            displayed.append(changed)
        if notice.value["event"] == "transcript.final":
            final_events += 1

    assert "Hello everyone" in displayed
    assert "Hello everyone, today we're" in displayed
    assert displayed.count("Hello everyone") == 1
    assert final_events == 2
    assert displayed[-1].endswith("real time.")


class _CredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def test_gui_api_configuration_is_stored_by_loopback_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DICTATION_CAPTION_TOKEN", raising=False)
    backend = _CredentialBackend()
    credentials = CaptionCredentials(backend)
    credentials.save("http://localhost:9000", "secret-token")
    assert credentials.endpoint() == "http://localhost:9000"
    assert credentials.token("http://localhost:9000") == "secret-token"
    credentials.forget("http://localhost:9000")
    assert credentials.token("http://localhost:9000") == ""


def _server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    if len(payload) < 126:
        header.append(len(payload))
    else:
        header.append(126)
        header.extend(struct.pack("!H", len(payload)))
    return bytes(header) + payload


def _read_client_frame(connection: socket.socket) -> tuple[int, bytes]:
    header = connection.recv(2)
    if len(header) != 2:
        raise ConnectionError("client frame ended early")
    first, second = header
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(connection, 8))[0]
    mask = _recv_exact(connection, 4)
    payload = _recv_exact(connection, length)
    return first & 0x0F, bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise ConnectionError("socket ended early")
        result.extend(chunk)
    return bytes(result)


class _ContractApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    token = "test-token"
    ping_seen = threading.Event()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._json(401, {"error": True, "reason": "unknown_or_revoked_token"})
            return
        if self.headers.get("Origin") is not None:
            self._json(403, {"error": True, "reason": "browser_origin_denied"})
            return
        if self.path == "/v1/status":
            self._json(
                200,
                {
                    "version": 1,
                    "capabilities": ["status:read", "transcript:live"],
                    "state": {
                        "session_id": "session-1",
                        "partials_available": True,
                    },
                },
            )
            return
        if self.path != "/v1/events":
            self._json(404, {"error": True, "reason": "unknown_endpoint"})
            return

        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()

        self.connection.settimeout(2)
        opcode, payload = _read_client_frame(self.connection)
        if opcode == 0x9:
            type(self).ping_seen.set()
            self.connection.sendall(_server_frame(0xA, payload))
        event = json.dumps(
            {
                "version": 1,
                "event": "transcript.partial",
                "session_id": "session-1",
                "segment_id": "segment-1",
                "revision": 1,
                "start_ms": 0,
                "end_ms": 500,
                "text": "hello live captions",
                "is_final": False,
                "privacy": "unredacted",
            }
        ).encode("utf-8")
        self.connection.sendall(_server_frame(0x1, event))
        self.close_connection = True

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def test_client_reads_status_and_streams_a_partial_from_contract_server() -> None:
    _ContractApiHandler.ping_seen.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContractApiHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    client = LocalApiClient(
        Endpoint.parse(f"http://127.0.0.1:{port}"), "test-token", timeout=0.2
    )
    try:
        assert validate_status(client.status()) == ("session-1", True)
        stream = client.events()
        event = next(stream)
        stream.close()
        assert _ContractApiHandler.ping_seen.wait(timeout=1)
        assert event["text"] == "hello live captions"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
