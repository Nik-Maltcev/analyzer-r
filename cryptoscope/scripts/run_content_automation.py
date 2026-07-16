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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_content_automation(deploy_preview=args.deploy_preview)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Content automation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
