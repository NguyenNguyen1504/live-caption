"""Small native client for the local version 1 HTTP/WebSocket API.

The project deliberately does not depend on a networking framework.  This
module implements only the RFC 6455 client behavior needed by the documented
event stream: an authenticated upgrade, server text frames, ping/pong, and a
masked close response.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import struct
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlsplit

MAX_HTTP_HEADER_BYTES = 16 * 1024
MAX_EVENT_BYTES = 1024 * 1024
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CaptionClientError(RuntimeError):
    """A safe-to-display client or protocol error."""


class ApiResponseError(CaptionClientError):
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"API error {status}: {reason}")


class WebSocketClosed(CaptionClientError):
    def __init__(self, code: int = 1000, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason or f"WebSocket closed ({code})")


class StreamStopped(CaptionClientError):
    """The owning GUI stopped or replaced this stream."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: str) -> Endpoint:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "ws"}:
            raise ValueError("endpoint must use http:// or ws://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, a query, or a fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("endpoint must not contain a path")
        host = parsed.hostname or ""
        if host.lower() != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError as error:
                raise ValueError("endpoint must be localhost or a loopback address") from error
            if not address.is_loopback:
                raise ValueError("endpoint must be a loopback address")
        try:
            port = parsed.port or 8765
        except ValueError as error:
            raise ValueError("endpoint has an invalid port") from error
        return cls(host=host, port=port)

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def status_url(self) -> str:
        return f"http://{self.authority}/v1/status"


class LocalApiClient:
    def __init__(self, endpoint: Endpoint, token: str, *, timeout: float = 3.0) -> None:
        if not token or "\r" in token or "\n" in token:
            raise ValueError("token is empty or invalid")
        try:
            token.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("token must contain ASCII characters only") from error
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        # A proxy must never receive the loopback API's bearer token, even if
        # the process inherited proxy-related environment variables.
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint.status_url,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with self._http.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise _http_error(error) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise CaptionClientError("caption service is unavailable") from error
        if not isinstance(payload, dict):
            raise CaptionClientError("caption service returned an invalid status")
        return payload

    def events(
        self, *, stop_event: threading.Event | None = None
    ) -> Iterator[dict[str, Any]]:
        with _WebSocket(
            self.endpoint, self.token, timeout=self.timeout, stop_event=stop_event
        ) as websocket:
            while True:
                message = websocket.receive_text()
                try:
                    event = json.loads(message)
                except json.JSONDecodeError as error:
                    raise CaptionClientError("caption service sent invalid JSON") from error
                if isinstance(event, dict):
                    yield event


def validate_status(payload: dict[str, Any]) -> tuple[str, bool]:
    """Validate the documented capabilities and return current session state."""
    if payload.get("version") != 1:
        raise CaptionClientError("unsupported caption API version")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or "status:read" not in capabilities:
        raise CaptionClientError("caption token lacks status:read")
    if "transcript:live" not in capabilities:
        raise CaptionClientError("caption token lacks transcript:live")
    state = payload.get("state")
    if not isinstance(state, dict):
        raise CaptionClientError("caption service returned an invalid status")
    session_id = state.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""
    partials_available = state.get("partials_available", True)
    if not isinstance(partials_available, bool):
        partials_available = True
    return session_id, partials_available


class _WebSocket:
    def __init__(
        self,
        endpoint: Endpoint,
        token: str,
        *,
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self._buffer = bytearray()
        self._fragment = bytearray()
        self._fragment_opcode: int | None = None
        self._awaiting_heartbeat = False
        self._ready = False
        self.stop_event = stop_event

    def __enter__(self) -> _WebSocket:
        try:
            self.socket = socket.create_connection(
                (self.endpoint.host, self.endpoint.port), timeout=self.timeout
            )
            self.socket.settimeout(self.timeout)
            self._handshake()
            self._ready = True
            return self
        except Exception:
            self.close(send=False)
            raise

    def __exit__(self, *exc: object) -> None:
        self.close(send=True)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /v1/events HTTP/1.1\r\n"
            f"Host: {self.endpoint.authority}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "\r\n"
        ).encode("ascii")
        assert self.socket is not None
        self.socket.sendall(request)
        header = self._read_until(b"\r\n\r\n", MAX_HTTP_HEADER_BYTES)
        head, _, remainder = header.partition(b"\r\n\r\n")
        self._buffer.extend(remainder)
        lines = head.decode("iso-8859-1").split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError) as error:
            raise CaptionClientError("caption service returned an invalid handshake") from error
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        if status != 101:
            reason = self._read_error_reason(headers)
            raise ApiResponseError(status, reason)
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode(
            "ascii"
        )
        if headers.get("sec-websocket-accept") != expected:
            raise CaptionClientError("caption service returned an invalid WebSocket handshake")

    def _read_error_reason(self, headers: dict[str, str]) -> str:
        try:
            length = min(int(headers.get("content-length", "0")), MAX_EVENT_BYTES)
        except ValueError:
            length = 0
        body = self._take(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8"))
            reason = payload.get("reason", "request_refused")
            return reason if isinstance(reason, str) else "request_refused"
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "request_refused"

    def receive_text(self) -> str:
        while True:
            fin, opcode, payload = self._receive_frame()
            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1000
                reason = payload[2:].decode("utf-8", errors="replace")
                self._send_frame(0x8, payload[:125])
                raise WebSocketClosed(code, reason)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1 and fin:
                return _decode_text(payload)
            if opcode == 0x1 and not fin:
                self._fragment_opcode = opcode
                self._fragment = bytearray(payload)
                continue
            if opcode == 0x0 and self._fragment_opcode == 0x1:
                self._fragment.extend(payload)
                if len(self._fragment) > MAX_EVENT_BYTES:
                    raise CaptionClientError("caption event is too large")
                if fin:
                    message = _decode_text(bytes(self._fragment))
                    self._fragment.clear()
                    self._fragment_opcode = None
                    return message

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._take(2)
        if first & 0x70:
            raise CaptionClientError("caption service sent an invalid WebSocket frame")
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if second & 0x80:
            raise CaptionClientError("caption service sent a masked frame")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._take(8))[0]
        if length > MAX_EVENT_BYTES:
            raise CaptionClientError("caption event is too large")
        if opcode >= 0x8 and (not fin or length > 125):
            raise CaptionClientError("caption service sent an invalid control frame")
        return fin, opcode, self._take(length)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.socket is None:
            return
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buffer:
            if len(self._buffer) >= limit:
                raise CaptionClientError("caption service returned an oversized response")
            self._buffer.extend(self._recv())
        end = self._buffer.index(marker) + len(marker)
        if end > limit:
            raise CaptionClientError("caption service returned an oversized response")
        result = bytes(self._buffer[:end])
        del self._buffer[:end]
        return result

    def _take(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._buffer.extend(self._recv())
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _recv(self) -> bytes:
        assert self.socket is not None
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise StreamStopped("caption stream stopped")
            try:
                chunk = self.socket.recv(8192)
            except socket.timeout as error:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise StreamStopped("caption stream stopped") from error
                if not self._ready:
                    raise CaptionClientError(
                        "caption service connection was interrupted"
                    ) from error
                if self._awaiting_heartbeat:
                    raise CaptionClientError(
                        "caption service connection was interrupted"
                    ) from error
                try:
                    self._send_frame(0x9, b"caption-heartbeat")
                except OSError as send_error:
                    raise CaptionClientError(
                        "caption service connection was interrupted"
                    ) from send_error
                self._awaiting_heartbeat = True
                continue
            except OSError as error:
                raise CaptionClientError("caption service connection was interrupted") from error
            if not chunk:
                raise WebSocketClosed(1006, "caption service disconnected")
            self._awaiting_heartbeat = False
            return chunk

    def close(self, *, send: bool) -> None:
        connection, self.socket = self.socket, None
        if connection is None:
            return
        if send:
            try:
                self.socket = connection
                self._send_frame(0x8, struct.pack("!H", 1000))
            except OSError:
                pass
            finally:
                self.socket = None
        try:
            connection.close()
        except OSError:
            pass


def _http_error(error: urllib.error.HTTPError) -> ApiResponseError:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        reason = payload.get("reason", "request_refused")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        reason = "request_refused"
    if not isinstance(reason, str):
        reason = "request_refused"
    return ApiResponseError(error.code, reason)


def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaptionClientError("caption service sent invalid text") from error
