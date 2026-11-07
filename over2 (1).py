import json
import threading
import time
import requests  # Added for fetching the remote JSON file
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# --- STRATEGY CONFIGURATION ---
ENTRY_THRESHOLD = 9.7           # Digits 0 and 1 must both be BELOW this percentage (< 9.7%)
TARGET_ENTRY_DIGITS = [0, 1]    # Valid entry digits to trade when conditions are met (updated to 0 and 1)

PREV_DIGIT_MIN = 5              # The 2nd-to-last quote digit must be between
PREV_DIGIT_MAX = 9              # PREV_DIGIT_MIN and PREV_DIGIT_MAX (5 to 9)

# --- LEAST DIGIT SPREAD/DIFFERENCE FILTER ---
LEAST_PCT_DIFF_MIN = 0.2        # Minimum percentage difference between 1st least and 2nd least appearing digits

# --- MOST DIGIT CONFIGURATION & SPREAD FILTER ---
MOST_DIGIT_MIN = 4              # 1st and 2nd most appearing digits must be between
MOST_DIGIT_MAX = 9              # MOST_DIGIT_MIN and MOST_DIGIT_MAX (4 to 9)
MOST_PCT_DIFF_MIN = 0.2         # Minimum percentage difference between 1st and 2nd most appearing digits

# --- STABILITY (CHOP) CONFIGURATION ---
CHOP_URL = "https://gist.githubusercontent.com/donovanbaraka25-sys/fa16beac3e64ad0a682713902d70d4e6/raw/95e52f6f008e2126d5b9d0dd19f347f6d6e66717/Chop.json"
CHOP_THRESHOLD = 50.0           # Stability value must be STRICTLY BELOW this number
CHOP_CHECK_INTERVAL = 60.0      # How often (in seconds) to fetch the updated stability value (set to 60s / 1 minute)

AUTO_TRADE = True               # Set to True to enable automatic button clicking
COOLDOWN_SECONDS = 3            # Pause between trade triggers
LOSS_HALT_SECONDS = 45          # Lockout duration after a losing trade

# Leave empty ("") to automatically click the main green Buy button,
# or set a word from the button text, e.g., "buy".
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


# --- STABILITY VALUE FETCHER ---
chop_state = {
    "value": None,
    "last_fetched": 0
}

def get_current_chop():
    """Fetches the stability value from the remote JSON file periodically."""
    global chop_state
    current_time = time.time()
    
    if current_time - chop_state["last_fetched"] > CHOP_CHECK_INTERVAL:
        try:
            response = requests.get(CHOP_URL, timeout=3)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    chop_val = data.get("chop", data.get("Chop", None))
                    if chop_val is not None:
                        chop_state["value"] = float(chop_val)
                        chop_state["last_fetched"] = current_time
        except Exception:
            pass
            
    return chop_state["value"]


# --- LIVE TICK FEED & CONTRACT SNIFFER ---
tick_state = {
    "quote": None,
    "digit": None,
    "prev_digit": None,
    "time": 0,
    "count": 0,
    "status": "not started",
}

contract_state = {
    "contract_id": None,
    "status": "idle",        # idle, open, won, lost
    "profit": 0.0,
    "total_wins": 0,
    "total_losses": 0,
    "net_profit": 0.0,
    "loss_cooldown_until": 0.0  # Timestamp tracking the 45s loss lockout
}


def start_performance_log_listener(driver):
    """Background thread that reads Chrome performance logs to capture live ticks and trade outcomes."""
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
                            
                            # 1. Parse Ticks & Quotes
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
                                        if len(quote_str) >= 2:
                                            digit = int(quote_str[-1])       
                                            prev_digit = int(quote_str[-2])  
                                            tick_state.update(
                                                prev_digit=prev_digit,
                                                quote=quote_str,
                                                digit=digit,
                                                time=time.time(),
                                                count=tick_state["count"] + 1,
                                                status="streaming",
                                            )

                            # 2. Parse Contract Results (Win / Loss Detection + 45s Halt)
                            if "proposal_open_contract" in payload or "buy" in payload:
                                data = json.loads(payload)
                                msg_type = data.get("msg_type")

                                # Capture buy confirmation
                                if msg_type == "buy":
                                    buy_info = data.get("buy", {})
                                    c_id = buy_info.get("contract_id")
                                    if c_id:
                                        contract_state["contract_id"] = c_id
                                        contract_state["status"] = "open"
                                        print(f"\n[+] Contract Purchased | ID: {c_id}")

                                # Capture final contract settlement updates
                                elif msg_type == "proposal_open_contract":
                                    poc = data.get("proposal_open_contract", {})
                                    if poc.get("is_sold") == 1:  # Contract settled
                                        profit = float(poc.get("profit", 0.0))
                                        contract_state["profit"] = profit
                                        contract_state["net_profit"] += profit

                                        if profit < 0:
                                            contract_state["status"] = "lost"
                                            contract_state["total_losses"] += 1
                                            contract_state["loss_cooldown_until"] = time.time() + LOSS_HALT_SECONDS
                                            print(f"\n[✗] TRADE RESULT: LOSS ❌ | Profit: {profit:.2f} | Net P/L: {contract_state['net_profit']:.2f}")
                                            print(f"[!] HALTING TRADING FOR {LOSS_HALT_SECONDS} SECONDS DUE TO LOSS.")
                                        else:
                                            contract_state["status"] = "won"
                                            contract_state["total_wins"] += 1
                                            print(f"\n[✓] TRADE RESULT: WIN ✅ | Profit: +{profit:.2f} | Net P/L: {contract_state['net_profit']:.2f}")

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

# Start background performance log tick & contract sniffer
start_performance_log_listener(driver)

# --- MONITORING LOOP ---
print("\n[ LIVE SPOT & 10-DIGIT PERCENTAGE TRACKER WITH LOSS/PROFIT DETECTION & 45S HALT ]")
print(f"Condition 1: Digits 0 and 1 must ALL be STRICTLY BELOW {ENTRY_THRESHOLD}% simultaneously")
print(f"Condition 2: Second-to-last quote digit must be between {PREV_DIGIT_MIN} and {PREV_DIGIT_MAX}")
print(f"Condition 3: Difference between 1st least and 2nd least appearing digits must be >= {LEAST_PCT_DIFF_MIN}%")
print(f"Condition 4: Most & 2nd Most appearing digits must be between {MOST_DIGIT_MIN} and {MOST_DIGIT_MAX}")
print(f"Condition 5: Difference between 1st most and 2nd most appearing digits must be >= {MOST_PCT_DIFF_MIN}%")
print(f"Condition 6: Stability must be < {CHOP_THRESHOLD} (Checked every 60s)")
print(f"Condition 7: Incoming last digit must match target entry digits {TARGET_ENTRY_DIGITS}")
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
            current_chop = get_current_chop()

            if stats:
                spot_str = f"Spot: {spot_price} (Last: {last_digit}, Prev/2nd-last: {prev_digit})" if spot_price else f"Spot: Waiting for ticks..."

                # Sort digits by percentage ascending for least appearing digits
                sorted_digits_asc = sorted(stats.keys(), key=lambda d: stats[d])
                least_digit = sorted_digits_asc[0]
                second_least_digit = sorted_digits_asc[1]
                
                # Sort digits by percentage descending for most appearing digits
                sorted_digits_desc = sorted(stats.keys(), key=lambda d: stats[d], reverse=True)
                most_digit = sorted_digits_desc[0]
                second_most_digit = sorted_digits_desc[1]

                digit_parts = []
                for d in range(10):
                    tag = ""
                    if d == most_digit:
                        tag = " [L]"
                    elif d == least_digit:
                        tag = " [S]"
                    digit_parts.append(f"[{d}]: {stats[d]:>4.1f}%{tag}")
                digits_output = " | ".join(digit_parts)

                # --- REQUIREMENT: Digits 0 and 1 must all be below ENTRY_THRESHOLD ---
                required_digits = [0, 1]
                all_below_threshold = all(stats[d] < ENTRY_THRESHOLD for d in required_digits)
                entry_str = f"Digits 0, 1 < {ENTRY_THRESHOLD}%: Active" if all_below_threshold else f"Digits 0, 1 < {ENTRY_THRESHOLD}%: Inactive (0: {stats[0]:.1f}%, 1: {stats[1]:.1f}%)"

                # Least Digits Diff Rule (Condition 3) - >= LEAST_PCT_DIFF_MIN
                diff_least_val = stats[second_least_digit] - stats[least_digit]
                rule_least_diff_met = diff_least_val >= LEAST_PCT_DIFF_MIN
                diff_least_str = f"Least Diff (≥{LEAST_PCT_DIFF_MIN}%): {diff_least_val:.1f}%"

                # Most Digits Range & Diff Rules (Conditions 4 & 5)
                most_range_ok = (MOST_DIGIT_MIN <= most_digit <= MOST_DIGIT_MAX) and (MOST_DIGIT_MIN <= second_most_digit <= MOST_DIGIT_MAX)
                diff_most_val = stats[most_digit] - stats[second_most_digit]
                most_diff_ok = diff_most_val >= MOST_PCT_DIFF_MIN
                rule_most_met = most_range_ok and most_diff_ok
                diff_most_str = f"Most Range [{MOST_DIGIT_MIN}-{MOST_DIGIT_MAX}]: {most_range_ok} | Most Diff (≥{MOST_PCT_DIFF_MIN}%): {diff_most_val:.1f}%"

                # Stability Rule Check (Condition 6)
                chop_ok = (current_chop is not None) and (current_chop < CHOP_THRESHOLD)
                chop_str = f"Stability: {current_chop} ({'Active' if chop_ok else 'Inactive'})"

                # Combined Strategy Trigger
                strategy_triggered = rule_least_diff_met and rule_most_met and chop_ok

                # Spot last digit matches entry digits (0 or 1) and 0, 1 are all below threshold
                spot_matches_entry = (last_digit is not None) and all_below_threshold and (last_digit in TARGET_ENTRY_DIGITS)

                # Second-to-last digit must be between PREV_DIGIT_MIN and PREV_DIGIT_MAX (5 to 9)
                prev_ok = (prev_digit is not None) and (PREV_DIGIT_MIN <= prev_digit <= PREV_DIGIT_MAX)

                current_time = time.time()
                score_summary = f"[Wins: {contract_state['total_wins']} | Losses: {contract_state['total_losses']} | Net P/L: {contract_state['net_profit']:.2f}]"

                # Check if 45s loss lockout is active
                in_loss_lockout = current_time < contract_state["loss_cooldown_until"]

                if in_loss_lockout:
                    halt_remaining = int(contract_state["loss_cooldown_until"] - current_time) + 1
                    signal_msg = f"⏳ LOSS HALT ACTIVE! Paused for {halt_remaining}s more | {score_summary}"
                elif strategy_triggered and spot_matches_entry and prev_ok:
                    if (current_time - last_trade_time) > COOLDOWN_SECONDS:
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! Spot last digit ({last_digit}) matched Entry | {score_summary}"
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
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! (Cooldown active: {remaining}s remaining) | {score_summary}"
                elif strategy_triggered and (last_digit in TARGET_ENTRY_DIGITS) and not all_below_threshold:
                    signal_msg = f"Entry digit {last_digit} hit but skipped (Digits 0, 1 must all be < {ENTRY_THRESHOLD}%) | {score_summary}"
                elif strategy_triggered and spot_matches_entry:
                    signal_msg = f"Entry digit {last_digit} hit but 2nd-to-last digit is {prev_digit} (needs {PREV_DIGIT_MIN}-{PREV_DIGIT_MAX}) - skipped | {score_summary}"
                elif (rule_least_diff_met and rule_most_met) and not chop_ok:
                    signal_msg = f"Waiting... Stability value is too high ({current_chop} >= {CHOP_THRESHOLD}) | {score_summary}"
                elif strategy_triggered:
                    signal_msg = f"Strategy Ready (Least Diff: {diff_least_val:.1f}%, Most Diff: {diff_most_val:.1f}%)... Waiting for Digits 0, 1 < {ENTRY_THRESHOLD}% & Spot Match | {score_summary}"
                else:
                    signal_msg = f"Waiting... (Least Met: {rule_least_diff_met} | Most Met: {rule_most_met} | Stability OK: {chop_ok}) | {score_summary}"

                # Print each new tick cleanly on its own line
                print(f"{spot_str}  ||  {digits_output}  ||  {entry_str}  ||  {diff_least_str}  ||  {diff_most_str}  ||  {chop_str}  ||  {signal_msg}")
            else:
                print(f"[!] {status}")

        time.sleep(LOOP_DELAY)

except KeyboardInterrupt:
    print(f"\n\n[!] Monitoring stopped by user. Final Stats -> Wins: {contract_state['total_wins']}, Losses: {contract_state['total_losses']}, Net P/L: {contract_state['net_profit']:.2f}")
