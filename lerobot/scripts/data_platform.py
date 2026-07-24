#!/usr/bin/env python
"""Compatibility entry point for :mod:`lerobot.data_platform`."""

import sys

from lerobot.data_platform import cli as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
