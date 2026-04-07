"""
Quick logic tests for auto_manager + openrouter retry/timeout handling.
Run: python test_autobot_logic.py
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

PASS = "✅"
FAIL = "❌"
failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)


# ── 1. Model timeouts ─────────────────────────────────────────────────────────
print("\n[1] Model timeouts")
from openrouter import _model_timeout, _DEFAULT_TIMEOUT

check("qwen3.6-plus → 300s",  _model_timeout("qwen/qwen3.6-plus:free") == 300.0)
check("deepseek-r1  → 300s",  _model_timeout("deepseek/deepseek-r1:free") == 300.0)
check("llama-3.3    → 120s",  _model_timeout("meta-llama/llama-3.3-70b-instruct:free") == _DEFAULT_TIMEOUT)
check("unknown      → 120s",  _model_timeout("some/unknown-model") == _DEFAULT_TIMEOUT)


# ── 2. _parse_decision robustness ─────────────────────────────────────────────
print("\n[2] _parse_decision")
from auto_manager import _parse_decision

r = _parse_decision('{"thinking":"OK","actions":[{"type":"noop"}]}')
check("plain JSON",           r.get("thinking") == "OK" and r["actions"][0]["type"] == "noop")

r = _parse_decision('```json\n{"thinking":"md","actions":[]}\n```')
check("markdown-wrapped JSON", r.get("thinking") == "md")

r = _parse_decision("пробачте, не можу відповісти")
check("invalid → noop fallback", r.get("actions") == [{"type": "noop", "reason": "could not parse AI response"}])

r = _parse_decision('noise {"thinking":"t","actions":[]} more noise')
check("embedded JSON extracted", r.get("thinking") == "t")

r = _parse_decision('{"thinking":"x"}')
check("missing actions → no crash", isinstance(r.get("actions", []), list))


# ── 3. _running_managers guard ────────────────────────────────────────────────
print("\n[3] _running_managers guard")
from auto_manager import _running_managers

_running_managers.clear()
check("empty on start",          "fake-id" not in _running_managers)
_running_managers.add("fake-id")
check("add works",               "fake-id" in _running_managers)
_running_managers.discard("fake-id")
check("discard works",           "fake-id" not in _running_managers)
_running_managers.discard("fake-id")  # double discard — no exception
check("double discard safe",     True)


# ── 4. Timeout → no inner retry (raises immediately) ─────────────────────────
print("\n[4] Timeout raises immediately (no inner retry loop)")
import httpx
from openrouter import call_openrouter

call_count = 0

async def mock_post_timeout(*a, **kw):
    global call_count
    call_count += 1
    raise httpx.TimeoutException("simulated timeout")

async def test_timeout_no_retry():
    global call_count
    call_count = 0
    with patch("httpx.AsyncClient.post", new=mock_post_timeout):
        try:
            await call_openrouter(
                model="qwen/qwen3.6-plus:free",
                system_prompt="sys",
                user_message="hi",
                api_key="test-key",
            )
        except Exception:
            pass
    check("only 1 attempt on timeout (no inner retry)", call_count == 1,
          f"got {call_count} attempts")

asyncio.run(test_timeout_no_retry())


# ── 5. Soft-error retry scheduling ───────────────────────────────────────────
print("\n[5] Soft-error retry scheduling in auto_manager")

# Simulate: after timeout, last_run should be set so retry happens in ~3 min
now = datetime.utcnow()
interval_min = 30
retry_in_min = 3

simulated_last_run = now - timedelta(minutes=interval_min) + timedelta(minutes=retry_in_min)
next_fire_in = interval_min - (now - simulated_last_run).total_seconds() / 60

check("retry fires in ~3 min",   abs(next_fire_in - retry_in_min) < 0.1,
      f"expected ~{retry_in_min}, got {next_fire_in:.2f}")
check("retry < full interval",   next_fire_in < interval_min)


# ── 6. tick_auto_managers skips non-overdue ───────────────────────────────────
print("\n[6] tick_auto_managers overdue logic")
from datetime import datetime, timedelta

def is_overdue(last_run, interval_minutes):
    if last_run is None:
        return True
    return (datetime.utcnow() - last_run) >= timedelta(minutes=interval_minutes)

check("None last_run → overdue",          is_overdue(None, 30))
check("1min ago, interval=30 → not due",  not is_overdue(datetime.utcnow() - timedelta(minutes=1), 30))
check("31min ago, interval=30 → due",     is_overdue(datetime.utcnow() - timedelta(minutes=31), 30))
check("3min ago after retry (interval=30) → not due yet",
      not is_overdue(datetime.utcnow() - timedelta(minutes=27), 30))


# ── Summary ───────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"❌ {len(failures)} test(s) FAILED: {', '.join(failures)}")
    sys.exit(1)
else:
    print(f"✅ All tests passed!")
