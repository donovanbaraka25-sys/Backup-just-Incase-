import json
import threading
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# --- STRATEGY CONFIGURATION ---
ENTRY_THRESHOLD = 10.0          # Digits 7, 8, and 9 must all be BELOW this percentage (< 10%)
TARGET_ENTRY_DIGITS = [8, 9]    # Valid entry digits to trade when conditions are met

PREV_DIGIT_MIN = 0              # The digit BEFORE the entry digit (8 or 9) must be between
PREV_DIGIT_MAX = 4              # PREV_DIGIT_MIN and PREV_DIGIT_MAX (0 to 4)

AUTO_TRADE = True               # Set to True to enable automatic button clicking
COOLDOWN_SECONDS = 3            # Pause between trade triggers

# Leave empty ("") to automatically click the main green Buy button,
# or set a word from the button text e.g. "buy".
BUTTON_KEYWORD = ""
LOOP_DELAY = 0.1                # Fast check for incoming network packets

# Connect to active Chrome instance with performance logging enabled (required to sniff live WS ticks)
chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

try:
    driver = webdriver.Chrome(options=chrome_options)
    print("[✓] Connected to live Chrome session.")
except Exception as e:
    print(f"[✗] Connection error: {e}")
    exit()


def switch_to_deriv_tab(driver):
    """Finds and switches to the DTrader tab."""
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            url = driver.current_url.lower()
            if "dtrader" in url or "deriv.com" in url:
                return True
        except Exception:
            continue
    return False


if switch_to_deriv_tab(driver):
    print(f"[✓] Using tab: {driver.title} | {driver.current_url}")
else:
    print("[✗] Couldn't find the DTrader tab. Open it in the debug Chrome window first.")
    exit()

# --- LIVE TICK FEED SNIFFER ---
tick_state = {
    "quote": None,
    "digit": None,
    "prev_digit": None,
    "time": 0,
    "count": 0,
    "status": "not started",
}


def start_performance_log_listener(driver):
    """Background thread that reads Chrome performance logs to capture live ticks."""
    def run():
        tick_state["status"] = "listening to browser network"
        while True:
            try:
                logs = driver.get_log("performance")
                for entry in logs:
                    try:
                        message = json.loads(entry["message"])["message"]
                        if message["method"] == "Network.webSocketFrameReceived":
                            payload = message["params"]["response"].get("payloadData", "")
                            if "quote" in payload or "tick" in payload:
                                data = json.loads(payload)
                                tick_data = None
                                if "tick" in data:
                                    tick_data = data["tick"]
                                elif data.get("msg_type") == "tick":
                                    tick_data = data["tick"]

                                if tick_data:
                                    quote_val = tick_data.get("quote")
                                    if quote_val is not None:
                                        quote_str = str(quote_val)
                                        digit = int(quote_str[-1])
                                        prev = tick_state["digit"]
                                        tick_state.update(
                                            prev_digit=prev,
                                            quote=quote_str,
                                            digit=digit,
                                            time=time.time(),
                                            count=tick_state["count"] + 1,
                                            status="streaming",
                                        )
                    except Exception:
                        pass
            except Exception as e:
                tick_state["status"] = f"log error: {str(e)[:50]}"
            time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()


def extract_all_digit_percentages(driver):
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
        raw_results = driver.execute_script(js_code)
        cleaned = [float(val.replace("%", "").strip()) for val in raw_results]
        if len(cleaned) >= 10:
            digit_stats = cleaned[-10:]
            return {d: digit_stats[d] for d in range(10)}, "ok"
        return None, f"Found only {len(cleaned)} percentage nodes (need 10). Make sure the stats panel is open."
    except Exception as e:
        return None, f"JS error: {str(e)[:100]}"


def list_purchase_buttons(driver):
    """Returns ONLY the main execution Buy button (ignoring Over/Under selection tabs)."""
    found = []
    for el in driver.find_elements(By.TAG_NAME, "button"):
        try:
            if not el.is_displayed():
                continue
            el_id = (el.get_attribute("id") or "")
            el_cls = (el.get_attribute("class") or "")
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


def execute_trade_click(driver):
    """Clicks the Buy purchase button."""
    buttons = list_purchase_buttons(driver)
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
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.click()
        return True, label
    except Exception as e1:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True, f"{label} (JS fallback)"
        except Exception as e2:
            return False, f"Click failed on {label}: {str(e1)[:60]} / {str(e2)[:60]}"


# Show detected purchase buttons on startup
print("[i] Purchase buttons detected right now:")
for _el, _label, _enabled in list_purchase_buttons(driver):
    print(f"    - {_label} | {'enabled' if _enabled else 'DISABLED'}")

# Start background performance log tick sniffer
start_performance_log_listener(driver)

# --- MONITORING LOOP ---
print("\n[ LIVE SPOT & 10-DIGIT PERCENTAGE TRACKER ]")
print("Condition 1: Most & 2nd Most appearing digits must be between 0 and 4")
print("Condition 2: Least appearing digit must be between 5 and 9")
print(f"Condition 3: Digits 7, 8, and 9 must ALL be STRICTLY BELOW {ENTRY_THRESHOLD}% simultaneously")
print(f"Condition 4: Digit BEFORE the entry digit must be between {PREV_DIGIT_MIN} and {PREV_DIGIT_MAX}")
print("=" * 120)

last_trade_time = 0
last_processed_count = 0

try:
    while True:
        current_count = tick_state["count"]

        # PROCESS ONLY WHEN A NEW TICK ARRIVES
        if current_count > last_processed_count:
            last_processed_count = current_count

            stats, status = extract_all_digit_percentages(driver)
            spot_price = tick_state["quote"]
            last_digit = tick_state["digit"]
            prev_digit = tick_state["prev_digit"]

            if stats:
                spot_str = f"Spot: {spot_price} (Last: {last_digit}, Prev: {prev_digit})" if spot_price else f"Spot: Waiting for ticks..."

                # Sort digits by percentage descending
                sorted_digits = sorted(stats.keys(), key=lambda d: stats[d], reverse=True)
                most_digit = sorted_digits[0]
                second_digit = sorted_digits[1]
                least_digit = min(stats, key=stats.get)

                digit_parts = []
                for d in range(10):
                    tag = ""
                    if d == most_digit:
                        tag = " [L]"
                    elif d == least_digit:
                        tag = " [S]"
                    digit_parts.append(f"[{d}]: {stats[d]:>4.1f}%{tag}")
                digits_output = " | ".join(digit_parts)

                # --- REQUIREMENT: Digits 7, 8, and 9 must all be below ENTRY_THRESHOLD ---
                required_digits = [7, 8, 9]
                all_below_threshold = all(stats[d] < ENTRY_THRESHOLD for d in required_digits)
                entry_str = f"Digits 7, 8, 9 < {ENTRY_THRESHOLD}%: Active" if all_below_threshold else f"Digits 7, 8, 9 < {ENTRY_THRESHOLD}%: Inactive (7: {stats[7]:.1f}%, 8: {stats[8]:.1f}%, 9: {stats[9]:.1f}%)"

                # Rule 1: Most and second most appearing digits are between 0 and 4
                rule1_met = (most_digit in range(0, 5)) and (second_digit in range(0, 5))

                # Rule 2: Least appearing digit is between 5 and 9
                rule2_met = least_digit in range(5, 10)

                # Combined Strategy Trigger
                strategy_triggered = rule1_met and rule2_met

                # Rule 3: Spot last digit matches entry digits (8 or 9) and 7, 8, 9 are all below threshold
                spot_matches_entry = (last_digit is not None) and all_below_threshold and (last_digit in TARGET_ENTRY_DIGITS)

                # Rule 4: Digit BEFORE the entry digit must be between PREV_DIGIT_MIN and PREV_DIGIT_MAX
                prev_ok = (prev_digit is not None) and (PREV_DIGIT_MIN <= prev_digit <= PREV_DIGIT_MAX)

                current_time = time.time()

                if strategy_triggered and spot_matches_entry and prev_ok:
                    if (current_time - last_trade_time) > COOLDOWN_SECONDS:
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! Spot ({last_digit}) matched Entry after prev digit {prev_digit}"
                        if AUTO_TRADE:
                            ok, detail = execute_trade_click(driver)
                            if ok:
                                print(f"\n[✓] {spot_str} | {signal_msg} -> ORDER EXECUTED! Clicked: {detail}")
                                last_trade_time = current_time
                            else:
                                print(f"\n[!] {spot_str} | {signal_msg} -> Click failed: {detail}")
                        else:
                            print(f"\n[DEMO MODE] {spot_str} | {signal_msg} -> Set AUTO_TRADE = True to click.")
                            last_trade_time = current_time
                    else:
                        remaining = int(COOLDOWN_SECONDS - (current_time - last_trade_time)) + 1
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! (Cooldown active: {remaining}s remaining)"
                elif strategy_triggered and (last_digit in TARGET_ENTRY_DIGITS) and not all_below_threshold:
                    signal_msg = f"Entry digit {last_digit} hit but skipped (Digits 7, 8, 9 must all be < {ENTRY_THRESHOLD}%)"
                elif strategy_triggered and spot_matches_entry:
                    signal_msg = f"Entry digit {last_digit} hit but previous digit is {prev_digit} (needs {PREV_DIGIT_MIN}-{PREV_DIGIT_MAX}) - skipped"
                elif strategy_triggered:
                    signal_msg = f"Strategy Ready (Top2 in 0-4: [{most_digit}, {second_digit}], Least in 5-9: [{least_digit}])... Waiting for Digits 7, 8, 9 < {ENTRY_THRESHOLD}% & Spot Match"
                else:
                    signal_msg = f"Waiting... (Top2 in 0-4: {rule1_met} | Least in 5-9: {rule2_met})"

                # Print each new tick cleanly on its own line
                print(f"{spot_str}  ||  {digits_output}  ||  {entry_str}  ||  {signal_msg}")
            else:
                print(f"[!] {status}")

        time.sleep(LOOP_DELAY)

except KeyboardInterrupt:
    print("\n\n[!] Monitoring stopped by user.")
