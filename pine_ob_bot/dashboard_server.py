from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class DashboardServer:
    """Small read-only HTTP server for generated forward-test reports."""

    def __init__(self, root: Path, host: str = "127.0.0.1", port: int = 8765):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        handler = partial(_QuietHandler, directory=str(self.root))
        self.server = ThreadingHTTPServer((host, port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="pine-ob-dashboard", daemon=True)
        actual_host, actual_port = self.server.server_address[:2]
        display_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
        self.url = f"http://{display_host}:{actual_port}/"
        self._write_index()

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _write_index(self) -> None:
        content = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30;url=paper_report_latest.html">
<title>Pine OB Live Dashboard</title>
<style>body{font-family:Segoe UI,Arial;background:#0b1020;color:#e7ebf5;padding:32px}
a{color:#49d39d;display:block;margin:12px 0}</style></head><body>
<h1>Pine OB Live Dashboard</h1><p>Opening the current daily report…</p>
<a href="paper_report_latest.html">Daily live report</a>
<a href="forward_test_cumulative.html">Cumulative forward test</a>
<a href="forward_test_daily_metrics.csv">Daily metrics CSV</a>
</body></html>"""
        (self.root / "index.html").write_text(content, encoding="utf-8")
