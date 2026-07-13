#!/usr/bin/env python3
"""project_learning_journal/serve_journal.py

سرور محلیِ آزمایشگاه تعاملی ژورنال آموزشی pine_ob_bot.

اجرا:
    python project_learning_journal/serve_journal.py
    (اختیاری) python project_learning_journal/serve_journal.py --port 8800

سپس آدرس چاپ‌شده (پیش‌فرض http://127.0.0.1:8766) را در مرورگر باز کنید؛ صفحه
به‌طور خودکار هم باز می‌شود.

معماری امنیتی (خلاصه؛ شرح کامل در فصل ۱۸ ژورنال):
  * هیچ کد پایتونی دلخواه از سمت کاربر اجرا نمی‌شود — نه eval، نه exec، نه
    subprocess/os.system، نه import پویا از رشتهٔ ورودی کاربر.
  * فقط experimentهای از پیش ثبت‌شده در lab_experiments.EXPERIMENTS قابل اجرا
    هستند؛ شناسهٔ experiment و هر ورودی، پیش از اجرا با schema اعتبارسنجی می‌شود.
  * درخواست GET فقط فایل‌های داخل همین پوشه (project_learning_journal) را سرو
    می‌کند، با بررسی صریح جلوگیری از path traversal.
  * هر اجرای experiment با یک timeout سخت (پیش‌فرض ۸ ثانیه) و سقف تعداد اجرای
    هم‌زمان محدود می‌شود.
  * هیچ اتصال شبکه/MT5/دمو برقرار نمی‌شود؛ هیچ فایلی خارج از
    project_learning_journal/runtime_data نوشته نمی‌شود.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

JOURNAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = JOURNAL_ROOT.parent

# pine_ob_bot و data (پکیج کمکیِ بارگذاری CSV) باید قابل import باشند.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from lab_experiments import EXPERIMENTS
except Exception as exc:  # pragma: no cover - startup diagnostics only
    print("خطا در بارگذاری lab_experiments.py — احتمالاً pine_ob_bot/pandas در دسترس نیست.")
    print(f"جزئیات: {exc}")
    EXPERIMENTS = {}

REQUEST_TIMEOUT_SECONDS = 8.0
MAX_CONCURRENT_RUNS = 4
MAX_BODY_BYTES = 200_000       # سقف اندازهٔ بدنهٔ درخواست (ضد سوءاستفادهٔ حافظه)
MAX_CANDLE_ROWS = 300
MAX_STRING_LEN = 128

_RUN_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)


def _json_safe(value):
    """Standard JSON has no Infinity/NaN; some handlers (e.g. profit factor with
    zero losses) can produce them. Replace with JSON-safe string markers so the
    browser's JSON.parse never breaks on a response."""
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# اعتبارسنجی ورودی — هرگز به کد کاربر اعتماد نمی‌شود
# ---------------------------------------------------------------------------

def _validate_scalar(field: dict, value):
    ftype = field["type"]
    name = field["name"]
    if ftype in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"«{name}» باید عدد باشد."
        if ftype == "int" and not float(value).is_integer():
            return None, f"«{name}» باید عدد صحیح باشد."
        value = int(value) if ftype == "int" else float(value)
        if "min" in field and value < field["min"]:
            return None, f"«{name}» نباید کمتر از {field['min']} باشد."
        if "max" in field and value > field["max"]:
            return None, f"«{name}» نباید بیشتر از {field['max']} باشد."
        return value, None
    if ftype == "bool":
        if not isinstance(value, bool):
            return None, f"«{name}» باید true/false باشد."
        return value, None
    if ftype == "select":
        if not isinstance(value, str) or value not in field.get("options", []):
            return None, f"«{name}» یک گزینهٔ نامعتبر است."
        return value, None
    if ftype == "str":
        if not isinstance(value, str):
            return None, f"«{name}» باید رشته باشد."
        if len(value) > MAX_STRING_LEN:
            return None, f"«{name}» بیش از حد طولانی است."
        return value, None
    return None, f"نوع فیلد ناشناخته برای «{name}»."


def validate_inputs(experiment_id: str, raw_inputs) -> tuple[dict | None, list[str]]:
    spec = EXPERIMENTS.get(experiment_id)
    if spec is None:
        return None, [f"آزمایش ناشناخته: {experiment_id}"]
    if not isinstance(raw_inputs, dict):
        return None, ["ورودی باید یک شیء JSON باشد."]

    allowed_names = {f["name"] for f in spec["fields"]}
    extra = set(raw_inputs) - allowed_names
    if extra:
        return None, [f"ورودی‌های غیرمجاز: {', '.join(sorted(extra))}"]

    cleaned = {}
    errors = []
    for field in spec["fields"]:
        name = field["name"]
        if name not in raw_inputs:
            errors.append(f"ورودی «{name}» ارسال نشده است.")
            continue
        value, err = _validate_scalar(field, raw_inputs[name])
        if err:
            errors.append(err)
        else:
            cleaned[name] = value
    return (cleaned if not errors else None), errors


# ---------------------------------------------------------------------------
# اجرای ایمنِ experiment (رجیستری-محور، با timeout و سقف هم‌زمانی)
# ---------------------------------------------------------------------------

def run_experiment(experiment_id: str, raw_inputs) -> dict:
    if experiment_id not in EXPERIMENTS:
        return {"success": False, "experiment_id": experiment_id,
               "error": f"آزمایش ناشناخته: {experiment_id}"}

    cleaned, errors = validate_inputs(experiment_id, raw_inputs)
    if errors:
        return {"success": False, "experiment_id": experiment_id,
               "error": " | ".join(errors)}

    handler = EXPERIMENTS[experiment_id]["handler"]
    acquired = _RUN_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        return {"success": False, "experiment_id": experiment_id,
               "error": "ظرفیت اجرای هم‌زمان پر است؛ چند لحظه بعد دوباره تلاش کنید."}
    try:
        start = time.perf_counter()
        future = _EXECUTOR.submit(handler, cleaned)
        try:
            outcome = future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            return {"success": False, "experiment_id": experiment_id,
                   "error": "اجرا بیش از حد مجاز طول کشید (Timeout)."}
        except Exception as exc:  # noqa: BLE001 - نمایش پیام خطای کنترل‌شده به کاربر
            return {"success": False, "experiment_id": experiment_id,
                   "error": f"اجرای آزمایش با خطا مواجه شد: {exc}"}
        elapsed_ms = (time.perf_counter() - start) * 1000
    finally:
        _RUN_SEMAPHORE.release()

    return {
        "success": True,
        "experiment_id": experiment_id,
        "inputs": cleaned,
        "result": outcome.get("result", {}),
        "steps": outcome.get("steps", []),
        "metrics": outcome.get("metrics", {}),
        "warnings": outcome.get("warnings", []),
        "execution_time_ms": round(elapsed_ms, 3),
    }


# ---------------------------------------------------------------------------
# HTTP handler: فایل‌های استاتیک + یک endpoint واحد POST /api/run
# ---------------------------------------------------------------------------

class JournalRequestHandler(BaseHTTPRequestHandler):
    server_version = "PineObBotLab/1.0"

    def log_message(self, format, *args):  # noqa: A002 - همنام با پایه، سکوت لاگ پرحجم
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(_json_safe(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_static_path(self, url_path: str) -> Path | None:
        if url_path in ("", "/"):
            url_path = "/00_index.html"
        rel = url_path.lstrip("/")
        candidate = (JOURNAL_ROOT / rel).resolve()
        try:
            candidate.relative_to(JOURNAL_ROOT)
        except ValueError:
            return None  # تلاش برای path traversal
        if candidate.name.startswith("_") or "runtime_data" in candidate.parts:
            return None  # فایل‌های داخلی/کاری، هرگز سرو نشوند
        if candidate.suffix == ".py":
            return None  # سورس پایتونیِ سرور/آزمایش‌ها هرگز از طریق HTTP قابل دانلود نباشد
        if not candidate.exists() or not candidate.is_file():
            return None
        return candidate

    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".csv": "text/csv; charset=utf-8", ".svg": "image/svg+xml",
    }

    def do_GET(self):  # noqa: N802 - امضای استاندارد http.server
        parsed = urlparse(self.path)
        path = self._safe_static_path(parsed.path)
        if path is None:
            self._send_json(404, {"error": "یافت نشد."})
            return
        content_type = self._CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            self._send_json(404, {"error": "مسیر نامعتبر."})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "اندازهٔ درخواست نامعتبر است."})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "JSON نامعتبر است."})
            return
        if not isinstance(payload, dict) or "experiment_id" not in payload:
            self._send_json(400, {"error": "فیلد experiment_id لازم است."})
            return
        experiment_id = payload.get("experiment_id")
        inputs = payload.get("inputs", {})
        if not isinstance(experiment_id, str) or experiment_id not in EXPERIMENTS:
            self._send_json(200, {"success": False, "experiment_id": experiment_id,
                                  "error": "آزمایش ناشناخته یا ثبت‌نشده."})
            return
        response = run_experiment(experiment_id, inputs)
        self._send_json(200, response)


def find_open_port(preferred: int, host: str, attempts: int = 20) -> int:
    import socket
    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("هیچ پورت آزادی پیدا نشد.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="سرور محلی آزمایشگاه تعاملی ژورنال pine_ob_bot")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    port = find_open_port(args.port, args.host)
    if port != args.port:
        print(f"پورت {args.port} در دسترس نبود؛ به‌جای آن از پورت {port} استفاده می‌شود.")

    server = ThreadingHTTPServer((args.host, port), JournalRequestHandler)
    url = f"http://{args.host}:{port}/00_index.html"
    print(f"ژورنال آموزشی pine_ob_bot در حال اجراست: {url}")
    print(f"تعداد آزمایش‌های ثبت‌شده: {len(EXPERIMENTS)}")
    print("برای توقف: Ctrl+C")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        _EXECUTOR.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
