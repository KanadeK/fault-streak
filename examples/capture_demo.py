"""Capture this disposable process only. No process attachment or signals."""

import argparse
import faulthandler
from pathlib import Path
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(
        description="Create two real faulthandler dumps in a new directory."
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    gate = threading.Event()
    ready = threading.Event()

    def waiting_worker():
        ready.set()
        gate.wait(10)

    worker = threading.Thread(target=waiting_worker)
    worker.start()
    try:
        if not ready.wait(2):
            raise RuntimeError("worker did not start")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            frame = sys._current_frames().get(worker.ident)
            if frame and frame.f_code.co_name == "wait":
                break
            time.sleep(0.001)
        else:
            raise RuntimeError("worker did not reach wait")
        for name in ("first.txt", "second.txt"):
            with (args.directory / name).open("wb") as output:
                faulthandler.dump_traceback(file=output, all_threads=True)
    finally:
        gate.set()
        worker.join(3)
    print("Saved two snapshots. They contain local file paths; review before sharing.")


if __name__ == "__main__":
    main()
