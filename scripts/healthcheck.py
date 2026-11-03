#!/usr/bin/env python3
"""Convenience CLI script invoking the application infrastructure healthcheck."""

import sys
from pathlib import Path

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.app.infrastructure.healthcheck import main  # noqa: E402

if __name__ == "__main__":
    main()
