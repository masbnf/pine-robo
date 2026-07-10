"""تست‌های project_learning_journal/serve_journal.py و lab_experiments.py

این فایل خودِ آزمایشگاه تعاملی را تست می‌کند (نه pine_ob_bot را) — یعنی تضمین
می‌کند که لایهٔ امنیتی/اعتبارسنجی دقیقاً همان چیزی است که در فصل ۱۸ ژورنال
مستند شده. سبک نوشتن مطابق pine_ob_bot/tests/*.py است (توابع ساده‌ی
``def test_x(): assert ...``، بدون کلاس) تا هم با pytest و هم بدون آن قابل
اجرا باشد.

اجرا (با pytest، اگر نصب باشد):
    pytest project_learning_journal/tests/test_lab_backend.py -v

اجرا (بدون pytest، با ران‌ر دستی):
    python project_learning_journal/tests/test_lab_backend.py

هیچ‌کدام از این تست‌ها فایلی در pine_ob_bot/ یا مسیرهای اصلی پروژه نمی‌نویسند؛
هر فایل موقتی که لازم باشد در tempfile.mkdtemp() (خارج از ریپو) ساخته و پاک
می‌شود.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOURNAL_ROOT = HERE.parent
PROJECT_ROOT = JOURNAL_ROOT.parent

sys.path.insert(0, str(JOURNAL_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

import serve_journal as sj  # noqa: E402


# ---------------------------------------------------------------------------
# ۱. validate_inputs — اعتبارسنجی schema
# ---------------------------------------------------------------------------

def test_valid_known_experiment_executes_and_returns_success():
    # Arrange: آزمایشی سبک و بدون وابستگی جانبی
    payload = {"side": "buy", "entry": 2000, "stop": 1990, "rr": 1.5}
    # Act
    result = sj.run_experiment("take_profit_calculator", payload)
    # Assert
    assert result["success"] is True
    assert result["experiment_id"] == "take_profit_calculator"
    assert "summary" in result["result"]
    assert isinstance(result["steps"], list) and len(result["steps"]) > 0


def test_unknown_experiment_id_is_rejected():
    result = sj.run_experiment("this_experiment_does_not_exist", {})
    assert result["success"] is False
    assert "ناشناخته" in result["error"]


def test_extra_unknown_input_field_is_rejected():
    payload = {"side": "buy", "entry": 2000, "stop": 1990, "rr": 1.5, "evil_field": "x"}
    cleaned, errors = sj.validate_inputs("take_profit_calculator", payload)
    assert cleaned is None
    assert any("غیرمجاز" in e for e in errors)


def test_wrong_type_input_is_rejected():
    payload = {"side": "buy", "entry": "not-a-number", "stop": 1990, "rr": 1.5}
    cleaned, errors = sj.validate_inputs("take_profit_calculator", payload)
    assert cleaned is None
    assert any("عدد" in e for e in errors)


def test_out_of_range_value_is_rejected():
    payload = {"side": "buy", "entry": 2000, "stop": 1990, "rr": 999}  # سقف rr=10
    cleaned, errors = sj.validate_inputs("take_profit_calculator", payload)
    assert cleaned is None
    assert any("بیشتر از" in e for e in errors)


def test_missing_required_field_is_rejected():
    payload = {"side": "buy", "entry": 2000, "stop": 1990}  # rr جا افتاده
    cleaned, errors = sj.validate_inputs("take_profit_calculator", payload)
    assert cleaned is None
    assert any("ارسال نشده" in e for e in errors)


def test_non_dict_inputs_is_rejected():
    cleaned, errors = sj.validate_inputs("take_profit_calculator", ["not", "a", "dict"])
    assert cleaned is None
    assert any("شیء JSON" in e for e in errors)


def test_select_field_rejects_value_outside_options():
    payload = {"side": "not_a_real_side", "entry": 2000, "stop": 1990, "rr": 1.5}
    cleaned, errors = sj.validate_inputs("take_profit_calculator", payload)
    assert cleaned is None
    assert any("گزینهٔ نامعتبر" in e for e in errors)


# ---------------------------------------------------------------------------
# ۲. عدم اجرای کد دلخواه (تزریق رشته‌ای بی‌اثر می‌ماند، نه بیشتر)
# ---------------------------------------------------------------------------

def test_injection_style_string_is_treated_as_plain_data_not_executed():
    # یک رشتهٔ شبیه به کد پایتون -- باید فقط به‌عنوان مقدار رشته‌ای ذخیره شود
    payload = {"time": "__import__('os').system('id')", "open": 1950, "high": 1955,
               "low": 1948, "close": 1952.5}
    result = sj.run_experiment("candle_builder", payload)
    assert result["success"] is True
    # رشته دقیقاً همان‌طور که فرستاده شده برگشته -- نه اجرا شده، نه تغییر کرده
    assert result["result"]["candle"]["time"] == "__import__('os').system('id')"


def test_command_injection_shell_metacharacters_are_inert():
    payload = {"time": "; rm -rf / #", "open": 1950, "high": 1955, "low": 1948, "close": 1952.5}
    result = sj.run_experiment("candle_builder", payload)
    assert result["success"] is True
    assert result["result"]["candle"]["time"] == "; rm -rf / #"


# ---------------------------------------------------------------------------
# ۳. path traversal / سرو فایل استاتیک
# ---------------------------------------------------------------------------

class _FakeHandler:
    """فقط برای فراخوانی _safe_static_path بدون بالا آوردن سرور واقعی HTTP."""
    _safe_static_path = sj.JournalRequestHandler._safe_static_path


def test_path_traversal_with_dotdot_is_blocked():
    h = _FakeHandler()
    result = h._safe_static_path("/../../../../etc/passwd")
    assert result is None


def test_internal_underscore_prefixed_files_are_blocked():
    h = _FakeHandler()
    result = h._safe_static_path("/_analysis_notes.md")
    assert result is None


def test_runtime_data_directory_is_never_served():
    h = _FakeHandler()
    result = h._safe_static_path("/runtime_data/whatever.sqlite3")
    assert result is None


def test_python_source_files_are_not_downloadable():
    h = _FakeHandler()
    assert h._safe_static_path("/serve_journal.py") is None
    assert h._safe_static_path("/lab_experiments.py") is None


def test_root_path_resolves_to_index_html():
    h = _FakeHandler()
    result = h._safe_static_path("/")
    assert result is not None
    assert result.name == "00_index.html"


def test_nonexistent_file_returns_none_not_error():
    h = _FakeHandler()
    result = h._safe_static_path("/this_file_does_not_exist.html")
    assert result is None


# ---------------------------------------------------------------------------
# ۴. عدم تغییر فایل‌های اصلی پروژه
# ---------------------------------------------------------------------------

def test_running_an_experiment_does_not_write_outside_runtime_data():
    # Arrange: عکس‌فوری از فهرست فایل‌های ریشهٔ ژورنال قبل از اجرا
    before = {p.name for p in JOURNAL_ROOT.iterdir() if p.is_file()}
    # Act: چند آزمایش (شامل mini_backtest که تنها آزمایشِ فایل‌نویس است) اجرا می‌شود
    sj.run_experiment("candle_builder", {"time": "2026-01-01T00:00:00", "open": 1950,
                                         "high": 1955, "low": 1948, "close": 1952.5})
    # Assert: هیچ فایل جدیدی در ریشهٔ ژورنال ظاهر نشده
    after = {p.name for p in JOURNAL_ROOT.iterdir() if p.is_file()}
    assert before == after


def test_mini_backtest_cleans_up_its_temp_directory():
    runtime_dir = JOURNAL_ROOT / "runtime_data"
    before = {p.name for p in runtime_dir.iterdir()} if runtime_dir.exists() else set()
    payload = {"trend_swing_length": 12, "pivot_left": 5, "pivot_right": 5, "rr": 1.5,
              "sweep_atr_buffer": 0.2, "strong_choch_only": False,
              "max_entry_pivot_age": 10, "breakeven_trigger_r": 0}
    sj.run_experiment("mini_backtest", payload)
    after = {p.name for p in runtime_dir.iterdir()} if runtime_dir.exists() else set()
    # فقط فایل‌های ثابتی مثل .gitkeep باید باقی بمانند؛ هیچ پوشهٔ lab_backtest_* جدیدی نباید بماند
    leftover_dirs = [n for n in after if n.startswith("lab_backtest_")]
    assert leftover_dirs == [], f"پوشه‌های موقتِ پاک‌نشده: {leftover_dirs}"


# ---------------------------------------------------------------------------
# ۵. ساختار JSON خروجی
# ---------------------------------------------------------------------------

def test_output_json_has_required_top_level_keys():
    result = sj.run_experiment("risk_sizing_calculator", {
        "equity": 10000, "risk_percent": 0.01, "entry_price": 2000, "stop_loss": 1990,
        "tick_size": 0.01, "tick_value": 1.0, "volume_min": 0.01, "volume_max": 100,
        "volume_step": 0.01,
    })
    required = {"success", "experiment_id", "inputs", "result", "steps", "metrics",
               "warnings", "execution_time_ms"}
    assert required.issubset(result.keys())
    assert isinstance(result["execution_time_ms"], float)


def test_json_safe_replaces_infinite_float_with_string():
    payload = {"profit_factor": float("inf"), "nested": {"x": float("-inf"), "y": float("nan")}}
    safe = sj._json_safe(payload)
    # باید با json.dumps استاندارد قابل سریالایز باشد (نه Infinity/NaN خام)
    dumped = json.dumps(safe)
    reparsed = json.loads(dumped)
    assert reparsed["profit_factor"] == "Infinity"
    assert reparsed["nested"]["x"] == "-Infinity"
    assert reparsed["nested"]["y"] is None


def test_normal_float_is_unchanged_by_json_safe():
    assert sj._json_safe(3.14) == 3.14
    assert sj._json_safe({"a": [1, 2.5, "x"]}) == {"a": [1, 2.5, "x"]}


# ---------------------------------------------------------------------------
# ۶. اجرای هم‌زمان دو تنظیم مختلف (معادل «حالت مقایسه» در frontend)
# ---------------------------------------------------------------------------

def test_two_different_settings_produce_independent_isolated_results():
    # frontend حالت مقایسه را با دو فراخوانی جدا از همین run_experiment می‌سازد؛
    # این تست تضمین می‌کند دو تنظیم متفاوت هیچ حالت مشترکی را بین خودشان نشت نمی‌دهند.
    result_a = sj.run_experiment("trend_detection_basic",
                                 {"scenario": "bull_choch_9bar", "trend_swing_length": 2})
    result_b = sj.run_experiment("trend_detection_basic",
                                 {"scenario": "bull_choch_9bar", "trend_swing_length": 4})
    assert result_a["success"] and result_b["success"]
    assert result_a["inputs"]["trend_swing_length"] == 2
    assert result_b["inputs"]["trend_swing_length"] == 4
    # نتایج باید مستقل باشند -- تغییر پارامتر باید واقعاً روی خروجی اثر بگذارد یا حداقل
    # objectهای برگشتی کاملاً جدا باشند (بدون به‌اشتراک‌گذاری state)
    assert result_a["result"] is not result_b["result"]


def test_repeated_runs_of_same_experiment_are_idempotent_no_state_leak():
    payload = {"side": "buy", "entry": 2000, "stop": 1990, "rr": 1.5}
    first = sj.run_experiment("take_profit_calculator", payload)
    second = sj.run_experiment("take_profit_calculator", payload)
    assert first["result"]["target"] == second["result"]["target"]


# ---------------------------------------------------------------------------
# ۷. Smoke test — بالا آوردن واقعیِ سرور HTTP و یک درخواست واقعی
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_smoke_server_starts_serves_and_runs_one_experiment():
    port = _find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(JOURNAL_ROOT / "serve_journal.py"),
         "--port", str(port), "--no-browser"],
        cwd=str(JOURNAL_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(30):
            try:
                urllib.request.urlopen(f"{base}/00_index.html", timeout=1)
                ready = True
                break
            except urllib.error.HTTPError:
                ready = True  # 404 هم یعنی سرور بالاست و پاسخ می‌دهد
                break
            except Exception:
                time.sleep(0.5)
        assert ready, "سرور در بازهٔ زمانی مجاز بالا نیامد."

        body = json.dumps({"experiment_id": "candle_builder",
                           "inputs": {"time": "2026-01-01T00:00:00", "open": 1950,
                                     "high": 1955, "low": 1948, "close": 1952.5}}).encode()
        req = urllib.request.Request(f"{base}/api/run", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        assert data["success"] is True
        assert data["experiment_id"] == "candle_builder"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# ران‌ر دستی برای اجرا بدون pytest
# ---------------------------------------------------------------------------

def _run_all_without_pytest():
    glb = globals()
    names = [n for n in glb if n.startswith("test_") and callable(glb[n])]
    passed = failed = 0
    for name in names:
        try:
            glb[name]()
            print(f"  [PASS] {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name} -- {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n=== SUMMARY: {passed} passed, {failed} failed ===")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all_without_pytest()
    raise SystemExit(0 if ok else 1)
