"""``python -m vtp.voice`` — the push-to-talk frontend.

    python -m vtp.voice              # speak and listen
    python -m vtp.voice --text       # type instead of speaking
    python -m vtp.voice --quiet      # spoken input, printed replies

Requires the voice extra (``uv sync --extra voice``) and the ``claude`` CLI on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from vtp.voice.loop import VoiceLoop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vtp.voice", description=__doc__)
    parser.add_argument(
        "--text", action="store_true", help="type utterances instead of speaking them"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print replies instead of speaking them"
    )
    parser.add_argument("--verbose", action="store_true", help="log what it is doing")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    loop = VoiceLoop(speak=not args.quiet, listen=not args.text)
    print(f"\n  {loop.explain_limits()}\n")

    try:
        asyncio.run(loop.run())
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except ImportError as exc:
        print(f"\n  The voice extra is not installed: {exc}", file=sys.stderr)
        print("  Install it with:  uv sync --extra voice", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
