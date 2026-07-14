#!/usr/bin/env python3
"""Run the daily crypto Telegram content workflow once."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.content.automation import run_content_automation


def main() -> int:
    try:
        result = run_content_automation()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Content automation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
