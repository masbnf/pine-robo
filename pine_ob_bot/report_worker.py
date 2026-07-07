from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable


class ReportWorker:
    """Serialize expensive CSV/HTML exports away from the trading loop."""

    def __init__(self, logger: logging.Logger):
        self.log = logger
        self.jobs: queue.Queue = queue.Queue(maxsize=2)
        self.thread = threading.Thread(target=self._run, name="pine-ob-reports", daemon=True)
        self.thread.start()

    def submit(self, callback: Callable, *args) -> bool:
        job = (callback, args)
        try:
            self.jobs.put_nowait(job)
            return True
        except queue.Full:
            # Keep trading responsive. A newer snapshot will be attempted on
            # the next refresh; the final shutdown report always waits.
            self.log.warning("report queue busy; skipped one intermediate refresh")
            return False

    def close(self) -> None:
        self.jobs.put(None)
        self.thread.join(timeout=60)

    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                callback, args = job
                callback(*args)
            except Exception:
                self.log.exception("background report export failed")
            finally:
                self.jobs.task_done()
