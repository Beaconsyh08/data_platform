#!/usr/bin/env python
"""Compatibility entry point for the data-platform viewer."""

import sys

from lerobot.data_platform import viewer as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
