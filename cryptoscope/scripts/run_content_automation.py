#!/usr/bin/env python3
"""Run the daily crypto Telegram content workflow once."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.content.automation import run_content_automation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--deploy-preview",
        action="store_true",
        help="Republish the latest active signal after a deploy when enabled.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--main-only",
        action="store_true",
        help="Publish the daily main signal without channel updates.",
    )
    mode.add_argument(
        "--updates-only",
        action="store_true",
        help="Publish queued daily updates without a new signal.",
    )
    mode.add_argument(
        "--backfill-only",
        action="store_true",
        help="Retry missing Threads media without publishing Telegram content.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_content_automation(
            deploy_preview=args.deploy_preview,
            publish_main=not (args.updates_only or args.backfill_only),
            publish_updates=not (args.main_only or args.backfill_only),
        )
        print(
            f"Content automation result: {json.dumps(result, ensure_ascii=False)}",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"Content automation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
