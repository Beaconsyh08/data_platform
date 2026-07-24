#!/usr/bin/env python3
"""Compatibility entry point for the data-platform dataset splitter."""

import sys

from lerobot.data_platform import split_dataset_by_episodes as _implementation


if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
