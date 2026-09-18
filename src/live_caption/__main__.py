"""Command-line entry point for the Windows caption overlay."""

from __future__ import annotations

import argparse
import getpass
import sys

from live_caption.client import Endpoint
from live_caption.credentials import (
    DEFAULT_ENDPOINT,
    CaptionCredentials,
    CredentialError,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="live-caption",
        description="Show live dictation captions in a Windows bottom-screen overlay.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="loopback API endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="start in Real API Mode instead of the default Demo Mode",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--set-token", action="store_true", help="prompt for and store a paired token"
    )
    action.add_argument("--forget-token", action="store_true", help="remove the stored token")
    args = parser.parse_args(argv)

    try:
        endpoint = Endpoint.parse(args.endpoint)
    except ValueError as error:
        parser.error(str(error))
    try:
        credentials = CaptionCredentials()
    except CredentialError:
        credentials = None

    if args.set_token:
        if credentials is None:
            print("error: the credential store is unavailable", file=sys.stderr)
            return 1
        token = getpass.getpass("Paired caption token (input hidden): ").strip()
        if not token:
            print("error: token cannot be empty", file=sys.stderr)
            return 1
        try:
            credentials.save(args.endpoint, token)
        except (CredentialError, ValueError) as error:
            print(
                f"error: {error}",
                file=sys.stderr,
            )
            return 1
        print(f"Caption token stored for {endpoint.authority}.")
        return 0

    if args.forget_token:
        if credentials is not None:
            credentials.forget(args.endpoint)
        print(f"Stored caption token removed for {endpoint.authority}.")
        return 0
    if sys.platform != "win32":
        print("error: the live-caption overlay currently supports Windows only", file=sys.stderr)
        return 1

    from live_caption.overlay import CaptionOverlay

    try:
        CaptionOverlay(
            credentials,
            initial_mode="real" if args.real else "demo",
        ).run()
    except KeyboardInterrupt:
        return 0
    except Exception as error:  # noqa: BLE001 - Tk and platform errors vary
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                f"The live-caption application could not start.\n\n{type(error).__name__}",
                "Local Dictation Captions",
                0x10,
            )
        except Exception:
            pass
        print(f"error: caption overlay failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
