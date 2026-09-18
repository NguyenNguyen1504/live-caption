"""Real-API configuration backed by the operating-system credential store."""

from __future__ import annotations

import os

from live_caption.client import Endpoint

SERVICE_NAME = "LocalDictation"
ENDPOINT_ACCOUNT = "caption-api-endpoint"
TOKEN_ACCOUNT_PREFIX = "caption-api-token:"
DEFAULT_ENDPOINT = "http://127.0.0.1:8765"


class CredentialError(RuntimeError):
    pass


class CaptionCredentials:
    def __init__(self, backend: object | None = None) -> None:
        if backend is None:
            try:
                import keyring
            except ImportError as error:
                raise CredentialError("the keyring dependency is unavailable") from error
            backend = keyring
        self.backend = backend

    def endpoint(self) -> str:
        try:
            value = self.backend.get_password(  # type: ignore[attr-defined]
                SERVICE_NAME, ENDPOINT_ACCOUNT
            )
        except Exception as error:  # noqa: BLE001 - keyring backends vary
            raise CredentialError("credential store read failed") from error
        return value or DEFAULT_ENDPOINT

    def token(self, endpoint: str) -> str:
        environment = os.environ.get("DICTATION_CAPTION_TOKEN", "").strip()
        if environment:
            return environment
        account = TOKEN_ACCOUNT_PREFIX + Endpoint.parse(endpoint).authority
        try:
            value = self.backend.get_password(SERVICE_NAME, account)  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001
            raise CredentialError("credential store read failed") from error
        return (value or "").strip()

    def save(self, endpoint: str, token: str = "") -> None:
        parsed = Endpoint.parse(endpoint)
        normalized = f"http://{parsed.authority}"
        try:
            self.backend.set_password(  # type: ignore[attr-defined]
                SERVICE_NAME, ENDPOINT_ACCOUNT, normalized
            )
            if token.strip():
                self.backend.set_password(  # type: ignore[attr-defined]
                    SERVICE_NAME,
                    TOKEN_ACCOUNT_PREFIX + parsed.authority,
                    token.strip(),
                )
        except Exception as error:  # noqa: BLE001
            raise CredentialError("credential store write failed") from error

    def forget(self, endpoint: str) -> None:
        account = TOKEN_ACCOUNT_PREFIX + Endpoint.parse(endpoint).authority
        try:
            self.backend.delete_password(SERVICE_NAME, account)  # type: ignore[attr-defined]
        except Exception:
            pass
