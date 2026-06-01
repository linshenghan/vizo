#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.ui_style_guardrails import scan_targets


def main() -> int:
    violations = scan_targets(PROJECT_ROOT)
    if not violations:
        print("ui style guardrails: ok")
        return 0
    print("ui style guardrails: violations detected", file=sys.stderr)
    for violation in violations:
        print(violation.format(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
