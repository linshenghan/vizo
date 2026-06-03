#!/usr/bin/env python3
"""Compatibility entry point for the legacy opus command."""

import signal

from vizo_core.opus import main


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    main()

