"""Optional bounded upload worker. No database scans, cleanup or historical migration by default."""

import argparse
import logging
import signal
import threading
from pathlib import Path

from lerobot.data_platform.artifact_store import ArchiveConfig, ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.interval < 10:
        parser.error("interval must be at least 10 seconds")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            config = ArchiveConfig.from_file(args.config)
            if config.enabled:
                store = ArtifactStore(config)
                store.flush(limit=20)
        except Exception:
            # SDK errors can contain signed request URLs. Keep credentials out of service logs.
            logging.error("Archive upload paused; inspect capacity, configuration and OSS availability")
        stop.wait(args.interval)


if __name__ == "__main__":
    main()
