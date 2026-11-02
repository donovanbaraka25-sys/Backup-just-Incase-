import json
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ===== STRATEGY CONFIG (only this block differs between Over 2 and Under 7) =====
STRATEGY_NAME = "OVER 2"

TOP2_MIN, TOP2_MAX = 5, 9           # Most AND 2nd most appearing digits must be in this range
LEAST_MIN, LEAST_MAX = 0, 2         # Least appearing digit must be in this range

WATCH_DIGITS = [0, 1, 2]            # These digits must ALL be below ENTRY_THRESHOLD
ENTRY_THRESHOLD = 10.0              # Percentage (strictly below)
TARGET_ENTRY_DIGITS = [0, 1]        # Spot last digit must be one of these

PREV_DIGIT_MIN, PREV_DIGIT_MAX = 5, 9   # Digit BEFORE the entry digit must be in this range

# Optional extra rule: at least N of these digits must be ABOVE a percentage.
# Leave ABOVE_CHECK_DIGITS empty ([]) to disable.
ABOVE_CHECK_DIGITS = []
ABOVE_CHECK_MIN_COUNT = 0
ABOVE_CHECK_PERCENT = 10.0
# ===== END STRATEGY CONFIG =====

# --- GENERAL SETTINGS ---
AUTO_TRADE = True              # False = demo (prints signals only). Set True to click Buy for real.
COOLDOWN_SECONDS = 3            # Pause between trade triggers
BUTTON_KEYWORD = ""             # Leave empty to click the first enabled Buy button, or e.g. "buy"
LOOP_DELAY = 0.1                # How often to check for new ticks
DOM_SETTLE_DELAY = 0.15         # Wait after a new tick so the stats panel can update
NO_TICK_WARNING_SECONDS = 15    # Warn if no ticks have been seen after this long
DEBUGGER_ADDRESS = "127.0.0.1:9222"


def pause_and_exit(message=None, code=1):
    """Keeps the console window open so errors can be read, then exits."""
    if message:
        print(message)
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
    sys.exit(code)


# --- CONNECT TO CHROME ---
chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

try:
    driver = webdriver.Chrome(options=chrome_options)
    print("[✓] Connected to live Chrome session.")
except Exception as e:
    pause_and_exit(
        f"[✗] Connection error: {e}\n"
        f"    Make sure Chrome was started with --remote-debugging-port=9222"
    )


def switch_to_deriv_tab(drv):
    """Finds and switches to the DTrader tab."""
    for handle in drv.window_handles:
        try:
            drv.switch_to.window(handle)
            url = drv.current_url.lower()
            if "dtrader" in url or "deriv.com" in url:
                return True
        except Exception:
            continue
    return False


if switch_to_deriv_tab(driver):
    print(f"[✓] Using tab: {driver.title} | {driver.current_url}")
else:
    pause_and_exit("[✗] Couldn't find the DTrader tab. Open it in the debug Chrome window first.")


# --- LIVE TICK FEED (read from Chrome performance log, same thread as the main loop) ---
tick_state = {
    "quote": None,
    "digit": None,
    "prev_digit": None,
    "count": 0,
    "time": 0.0,
}


def parse_tick(payload):
    """Returns (quote_string, last_digit) from a websocket payload, or None."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tick = data.get("tick")
    if not isinstance(tick, dict):
        return None
    quote = tick.get("quote")
    if quote is None:
        return None

    # Use pip_size so trailing zeros are kept (6543.210 must not become 6543.21)
    pip_size = tick.get("pip_size")
    try:
        if isinstance(pip_size, int) and pip_size >= 0:
            quote_str = f"{float(quote):.{pip_size}f}"
        else:
            quote_str = str(quote)
    except (TypeError, ValueError):
        return None

    if not quote_str or not quote_str[-1].isdigit():
        return None
    return quote_str, int(quote_str[-1])


def poll_ticks(drv):
    """Reads any new websocket frames and updates tick_state. Returns the number of new ticks."""
    new_ticks = 0
    try:
        logs = drv.get_log("performance")
    except Exception:
        return 0

    for entry in logs:
        try:
            message = json.loads(entry["message"])["message"]
            if message.get("method") != "Network.webSocketFrameReceived":
                continue
            payload = message["params"]["response"].get("payloadData", "")
            if '"tick"' not in payload:
                continue
            parsed = parse_tick(payload)
        except Exception:
            continue

        if parsed is None:
            continue

        quote_str, digit = parsed
        tick_state["prev_digit"] = tick_state["digit"]
        tick_state["quote"] = quote_str
        tick_state["digit"] = digit
        tick_state["time"] = time.time()
        tick_state["count"] += 1
        new_ticks += 1

    return new_ticks


# --- DIGIT PERCENTAGES FROM THE PAGE ---
def extract_all_digit_percentages(drv):
    """Pulls all 10 digit percentages from the DTrader DOM stats table."""
    js_code = """
    let allNodes = Array.from(document.querySelectorAll('*'));
    let pctNodes = allNodes.filter(el => {
        let text = el.innerText ? el.innerText.trim() : '';
        return /^\\d+(\\.\\d+)?%$/.test(text) && el.children.length === 0;
    });
    return pctNodes.map(el => el.innerText.trim());
    """
    try:
        raw_results = drv.execute_script(js_code)
        cleaned = [float(val.replace("%", "").strip()) for val in raw_results]
        if len(cleaned) >= 10:
            digit_stats = cleaned[-10:]
            return {d: digit_stats[d] for d in range(10)}, "ok"
        return None, f"Found only {len(cleaned)} percentage nodes (need 10). Make sure the stats panel is open."
    except Exception as e:
        return None, f"JS error: {str(e)[:100]}"


# --- BUY BUTTON ---
def list_purchase_buttons(drv):
    """Returns the visible Buy/purchase buttons as (element, label, enabled)."""
    found = []
    for el in drv.find_elements(By.TAG_NAME, "button"):
        try:
            if not el.is_displayed():
                continue
            el_id = el.get_attribute("id") or ""
            el_cls = el.get_attribute("class") or ""
            text = (el.text or "").strip().replace("\n", " ")
            text_lower = text.lower()

            looks_like_purchase = (
                "purchase" in el_id.lower()
                or "btn-purchase" in el_cls
                or text_lower.startswith("buy")
                or "payout" in text_lower
            )

            if looks_like_purchase:
                enabled = el.is_enabled() and el.get_attribute("disabled") is None
                found.append((el, f"id='{el_id}' text='{text}'", enabled))
        except Exception:
            continue
    return found


def execute_trade_click(drv):
    """Clicks the Buy purchase button."""
    buttons = list_purchase_buttons(drv)
    if not buttons:
        return False, "No Buy purchase buttons found on the page"

    if BUTTON_KEYWORD:
        kw = BUTTON_KEYWORD.lower()
        buttons = [b for b in buttons if kw in b[1].lower()]
        if not buttons:
            return False, f"No purchase button matches keyword '{BUTTON_KEYWORD}'"

    enabled = [b for b in buttons if b[2]]
    if not enabled:
        return False, "Buy button found but disabled: " + " | ".join(b[1] for b in buttons)

    el, label, _ = enabled[0]
    try:
        drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.click()
        return True, label
    except Exception as e1:
        try:
            drv.execute_script("arguments[0].click();", el)
            return True, f"{label} (JS fallback)"
        except Exception as e2:
            return False, f"Click failed on {label}: {str(e1)[:60]} / {str(e2)[:60]}"


# --- DECISION LOGIC ---
def evaluate(stats, last_digit, prev_digit, last_trade_time, now):
    """Returns (signal_msg, should_trade, digits_output, entry_str)."""
    sorted_digits = sorted(range(10), key=lambda d: stats[d], reverse=True)
    most_digit = sorted_digits[0]
    second_digit = sorted_digits[1]
    least_digit = min(range(10), key=lambda d: stats[d])

    digit_parts = []
    for d in range(10):
        tag = ""
        if d == most_digit:
            tag = " [L]"
        elif d == least_digit:
            tag = " [S]"
        digit_parts.append(f"[{d}]: {stats[d]:>4.1f}%{tag}")
    digits_output = " | ".join(digit_parts)

    top2_ok = (TOP2_MIN <= most_digit <= TOP2_MAX) and (TOP2_MIN <= second_digit <= TOP2_MAX)
    least_ok = LEAST_MIN <= least_digit <= LEAST_MAX
    watch_ok = all(stats[d] < ENTRY_THRESHOLD for d in WATCH_DIGITS)

    if ABOVE_CHECK_DIGITS:
        above_count = sum(1 for d in ABOVE_CHECK_DIGITS if stats[d] > ABOVE_CHECK_PERCENT)
        above_ok = above_count >= ABOVE_CHECK_MIN_COUNT
    else:
        above_count = 0
        above_ok = True

    setup_ok = top2_ok and least_ok and above_ok
    entry_digit_ok = (last_digit is not None) and (last_digit in TARGET_ENTRY_DIGITS)
    prev_ok = (prev_digit is not None) and (PREV_DIGIT_MIN <= prev_digit <= PREV_DIGIT_MAX)

    watch_str = ", ".join(f"{d}: {stats[d]:.1f}%" for d in WATCH_DIGITS)
    entry_str = (
        f"Digits {WATCH_DIGITS} < {ENTRY_THRESHOLD}%: Active"
        if watch_ok
        else f"Digits {WATCH_DIGITS} < {ENTRY_THRESHOLD}%: Inactive ({watch_str})"
    )

    if not setup_ok:
        msg = (
            f"Waiting... (Top2 in {TOP2_MIN}-{TOP2_MAX}: {top2_ok} | "
            f"Least in {LEAST_MIN}-{LEAST_MAX}: {least_ok}"
        )
        if ABOVE_CHECK_DIGITS:
            msg += (
                f" | {ABOVE_CHECK_MIN_COUNT}+ of {ABOVE_CHECK_DIGITS} above "
                f"{ABOVE_CHECK_PERCENT}%: {above_ok} [{above_count}]"
            )
        msg += ")"
        return msg, False, digits_output, entry_str

    if not entry_digit_ok:
        msg = (
            f"Strategy Ready (Top2: [{most_digit}, {second_digit}], Least: [{least_digit}])... "
            f"Waiting for entry digit in {TARGET_ENTRY_DIGITS}"
        )
        return msg, False, digits_output, entry_str

    if not watch_ok:
        msg = f"Entry digit {last_digit} hit but skipped (Digits {WATCH_DIGITS} must all be < {ENTRY_THRESHOLD}%)"
        return msg, False, digits_output, entry_str

    if not prev_ok:
        msg = (
            f"Entry digit {last_digit} hit but previous digit is {prev_digit} "
            f"(needs {PREV_DIGIT_MIN}-{PREV_DIGIT_MAX}) - skipped"
        )
        return msg, False, digits_output, entry_str

    if (now - last_trade_time) <= COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - last_trade_time)) + 1
        msg = f"🔥 FULL TRIGGER! (Cooldown active: {remaining}s remaining)"
        return msg, False, digits_output, entry_str

    msg = f"🔥 FULL EXECUTION TRIGGER! Spot ({last_digit}) matched Entry after prev digit {prev_digit}"
    return msg, True, digits_output, entry_str


def main():
    print("[i] Purchase buttons detected right now:")
    for _el, _label, _enabled in list_purchase_buttons(driver):
        print(f"    - {_label} | {'enabled' if _enabled else 'DISABLED'}")

    print(f"\n[ {STRATEGY_NAME} | LIVE SPOT & 10-DIGIT PERCENTAGE TRACKER ]")
    print(f"Condition 1: Most & 2nd most appearing digits must be between {TOP2_MIN} and {TOP2_MAX}")
    print(f"Condition 2: Least appearing digit must be between {LEAST_MIN} and {LEAST_MAX}")
    print(f"Condition 3: Digits {WATCH_DIGITS} must ALL be STRICTLY BELOW {ENTRY_THRESHOLD}%")
    print(f"Condition 4: Spot last digit must be in {TARGET_ENTRY_DIGITS}")
    print(f"Condition 5: Digit BEFORE the entry digit must be between {PREV_DIGIT_MIN} and {PREV_DIGIT_MAX}")
    if ABOVE_CHECK_DIGITS:
        print(
            f"Condition 6: At least {ABOVE_CHECK_MIN_COUNT} of digits {ABOVE_CHECK_DIGITS} "
            f"must be ABOVE {ABOVE_CHECK_PERCENT}%"
        )
    print(f"AUTO_TRADE is {'ON - real Buy clicks will happen' if AUTO_TRADE else 'OFF (demo mode, no clicks)'}")
    print("=" * 120)

    last_trade_time = 0.0
    last_warning_time = time.time()

    while True:
        new_ticks = poll_ticks(driver)

        if new_ticks:
            time.sleep(DOM_SETTLE_DELAY)

            # Snapshot once so digit and prev_digit always belong together
            spot_price = tick_state["quote"]
            last_digit = tick_state["digit"]
            prev_digit = tick_state["prev_digit"]

            stats, status = extract_all_digit_percentages(driver)
            if not stats:
                print(f"[!] {status}")
            else:
                spot_str = f"Spot: {spot_price} (Last: {last_digit}, Prev: {prev_digit})"
                now = time.time()
                signal_msg, should_trade, digits_output, entry_str = evaluate(
                    stats, last_digit, prev_digit, last_trade_time, now
                )

                if should_trade:
                    if AUTO_TRADE:
                        ok, detail = execute_trade_click(driver)
                        if ok:
                            signal_msg += f" -> ORDER EXECUTED! Clicked: {detail}"
                            last_trade_time = now
                        else:
                            signal_msg += f" -> Click failed: {detail}"
                    else:
                        signal_msg = f"[DEMO MODE] {signal_msg} -> Set AUTO_TRADE = True to click."
                        last_trade_time = now

                print(f"{spot_str}  ||  {digits_output}  ||  {entry_str}  ||  {signal_msg}")

        elif tick_state["count"] == 0 and (time.time() - last_warning_time) > NO_TICK_WARNING_SECONDS:
            print(
                "[!] No ticks received yet. Check that the DTrader tab is streaming, and that Chrome "
                "was started with --remote-debugging-port=9222 (performance logging may not be "
                "active on an already-running Chrome; restart it with the debug flag)."
            )
            last_warning_time = time.time()

        time.sleep(LOOP_DELAY)


try:
    main()
except KeyboardInterrupt:
    print("\n\n[!] Monitoring stopped by user.")
except Exception as e:
    print(f"\n[✗] Unexpected error: {type(e).__name__}: {e}")
finally:
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
