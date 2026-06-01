#!/usr/bin/env python3
"""Vizo 主 CLI 入口，复用 opus.py 主实现。"""

import os
import signal
import sys
from pathlib import Path

os.environ.setdefault("OPUS_CMD_NAME", "vizo")
os.environ.setdefault("OPUS_BRAND_DISPLAY", "Vizo")
os.environ.setdefault("OPUS_COMPAT_ENTRY", "0")
sys.argv[0] = "vizo"

sys.path.insert(0, str(Path(__file__).parent))

from opus import main


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    main()
