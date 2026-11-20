import json
import threading
import time
import urllib.request
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# --- STRATEGY CONFIGURATION ---
ENTRY_THRESHOLD = 12.7          # Digits 8 and 9 must both be BELOW this percentage
TARGET_ENTRY_DIGITS = [8, 9]    # Valid entry digits to trade when conditions are met

PREV_DIGIT_MIN = 0              # The 2nd-to-last quote digit must be between
PREV_DIGIT_MAX = 4              # PREV_DIGIT_MIN and PREV_DIGIT_MAX (0 to 4)

# --- LEAST DIGIT SPREAD/DIFFERENCE FILTER ---
LEAST_PCT_DIFF_MIN = 0.1        # Min % difference between 1st least and 2nd least appearing digits (> 0.2%)

# --- CHOP / STABILITY CONFIGURATION (read from the TradingView/chart tab) ---
CHOP_MIN = 35.0                 # CHOP must be between CHOP_MIN and CHOP_MAX (inclusive)
CHOP_MAX = 50.0

# --- CCI CONFIGURATION (read from the TradingView/chart tab) ---
USE_CCI = True                  # Set to False to ignore CCI completely
CCI_MIN = -130.0                # CCI must be between CCI_MIN and CCI_MAX (inclusive)
CCI_MAX = -40.0

# --- ADX CONFIGURATION (read from the TradingView/chart tab) ---
USE_ADX = True                  # Set to False to ignore ADX completely
ADX_MIN = 25.0                  # ADX must be between ADX_MIN and ADX_MAX (inclusive)
ADX_MAX = 50.0

TV_CHECK_INTERVAL = 15.0        # Seconds between chart tab checks (CHOP + CCI + ADX read together)
TV_TAB_KEYWORDS = ["tradingview", "charts.deriv"]   # URL fragments that identify the chart tab

AUTO_TRADE = True               # Set to True to enable automatic button clicking
COOLDOWN_SECONDS = 3            # Pause between trade triggers
LOSS_HALT_SECONDS = 10          # Lockout duration after a losing trade

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


# --- TAB MANAGEMENT ---
# Only one thread may drive the browser at a time, because tab switching affects every call.
driver_lock = threading.RLock()
tab_handles = {"dtrader": None, "tv": None}


def find_tabs(driver):
    """Finds the DTrader tab and chart tab via Chrome's debug endpoint (no tab switching, so nothing can hang)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3) as r:
            targets = json.loads(r.read().decode())
    except Exception as e:
        print(f"[!] Couldn't read the tab list: {e}")
        return False

    handles = driver.window_handles

    def handle_for(target_id):
        for h in handles:
            if target_id in h:
                return h
        return None

    for t in targets:
        if t.get("type") != "page":
            continue
        url = t.get("url", "").lower()
        h = handle_for(t.get("id", ""))
        if not h:
            continue
        if "dtrader" in url and not tab_handles["dtrader"]:
            tab_handles["dtrader"] = h
        elif any(k in url for k in TV_TAB_KEYWORDS) and not tab_handles["tv"]:
            tab_handles["tv"] = h

    if tab_handles["dtrader"]:
        driver.switch_to.window(tab_handles["dtrader"])
        return True
    return False


with driver_lock:
    found_dtrader = find_tabs(driver)

if found_dtrader:
    with driver_lock:
        print(f"[✓] Using tab: {driver.title} | {driver.current_url}")
else:
    print("[✗] Couldn't find the DTrader tab. Open it in the debug Chrome window first.")
    exit()

if tab_handles["tv"]:
    print("[✓] TradingView/chart tab found.")
else:
    print("[!] TradingView/chart tab NOT found. CHOP/CCI/ADX can't be read, so no trades will fire until it is open.")


# --- CHART TAB INDICATOR CHECK (CHOP + CCI + ADX) ---
tv_state = {
    "chop": None,
    "cci": None,
    "adx": None,
    "last_check": 0.0,
}

# Finds the legend row for an indicator, e.g. "CHOP 14 54.83", "CCI 20 −226.30" or "ADX 14 14 27.45".
# Special minus/dash characters (U+2212, U+2013, U+2014) are converted to a normal "-" first,
# so negative values keep their sign.
# `pref` is the position of the value among the numbers after the indicator name:
#   CHOP / CCI have one period number   -> value is index 1
#   ADX has two (smoothing + DI length) -> value is index 2
# A row with too few numbers (e.g. just the title "ADX 14 14") is skipped, so the search
# climbs to the parent row that includes the actual value.
TV_INDICATORS_JS = """
function readIndicator(name, lo, hi, pref) {
    const re = new RegExp(name, 'i');
    let candidates = Array.from(document.querySelectorAll('div, span'))
        .filter(el => {
            const t = (el.innerText || '');
            return re.test(t) && t.length < 80;
        });
    candidates.sort((a, b) => a.innerText.length - b.innerText.length);
    for (const el of candidates) {
        let node = el;
        for (let i = 0; i < 5 && node; i++) {
            const t = (node.innerText || '').replace(/[\\u2212\\u2013\\u2014]/g, '-').replace(/\\s+/g, ' ').trim();
            if (t.length >= 80) break;
            const idx = t.toLowerCase().indexOf(name.toLowerCase());
            if (idx >= 0) {
                const nums = t.slice(idx).match(/-?\\d+(?:\\.\\d+)?/g);
                if (nums && nums.length > pref) {
                    const v = parseFloat(nums[pref]);
                    if (!isNaN(v) && v >= lo && v <= hi) return {v: v, t: t};
                }
            }
            node = node.parentElement;
        }
    }
    return null;
}
return {
    chop: readIndicator('chop', 0, 100, 1),
    cci: readIndicator('cci', -1000, 1000, 1),
    adx: readIndicator('adx', 0, 100, 2)
};
"""


def needed_keys():
    keys = ["chop"]
    if USE_CCI:
        keys.append("cci")
    if USE_ADX:
        keys.append("adx")
    return keys


def scan_frames_for_indicators(driver, found, depth=0):
    """Reads indicators from the current document, then searches iframes until everything needed is found."""
    try:
        res = driver.execute_script(TV_INDICATORS_JS)
    except Exception:
        res = None
    if res:
        for k in ("chop", "cci", "adx"):
            if found.get(k) is None and res.get(k) is not None:
                found[k] = res[k]

    if all(found.get(k) is not None for k in needed_keys()) or depth >= 3:
        return

    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        return

    for frame in frames:
        try:
            driver.switch_to.frame(frame)
        except Exception:
            continue
        try:
            scan_frames_for_indicators(driver, found, depth + 1)
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                pass
        if all(found.get(k) is not None for k in needed_keys()):
            return


def check_tradingview_tab(driver):
    """Switches to the chart tab, reads CHOP (and CCI/ADX), then switches back to DTrader."""
    if not tab_handles["tv"] or not tab_handles["dtrader"]:
        return None

    found = {"chop": None, "cci": None, "adx": None}
    with driver_lock:
        try:
            driver.switch_to.window(tab_handles["tv"])
            driver.switch_to.default_content()
            time.sleep(0.3)  # give the legend a moment to render
            scan_frames_for_indicators(driver, found)
        except Exception as e:
            print(f"\n[!] Chart tab check failed: {str(e)[:100]}")
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            try:
                driver.switch_to.window(tab_handles["dtrader"])
                driver.switch_to.default_content()
            except Exception:
                pass
    return found


def refresh_indicators():
    """Refreshes CHOP, CCI and ADX from the chart tab every TV_CHECK_INTERVAL seconds."""
    now = time.time()
    if now - tv_state["last_check"] > TV_CHECK_INTERVAL:
        tv_state["last_check"] = now
        found = check_tradingview_tab(driver)
        if found is None:
            return

        parts = []
        if found.get("chop") is not None:
            tv_state["chop"] = float(found["chop"]["v"])
            parts.append(f"CHOP: {tv_state['chop']} ('{found['chop']['t']}')")
        else:
            print("\n[!] CHOP not found on chart tab (is the CHOP indicator on the chart?)")

        if USE_CCI:
            if found.get("cci") is not None:
                tv_state["cci"] = float(found["cci"]["v"])
                parts.append(f"CCI: {tv_state['cci']} ('{found['cci']['t']}')")
            else:
                print("\n[!] CCI not found on chart tab (add the CCI indicator to the chart via Indicators)")

        if USE_ADX:
            if found.get("adx") is not None:
                tv_state["adx"] = float(found["adx"]["v"])
                parts.append(f"ADX: {tv_state['adx']} ('{found['adx']['t']}')")
            else:
                print("\n[!] ADX not found on chart tab (add the ADX indicator to the chart via Indicators)")

        if parts:
            print("\n[TV] Updated from chart tab -> " + " | ".join(parts))


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
    "loss_cooldown_until": 0.0  # Timestamp tracking when the loss penalty lifts
}


def start_performance_log_listener(driver):
    """Background thread that reads Chrome performance logs to capture live ticks and trade outcomes."""
    def run():
        tick_state["status"] = "listening to browser network"
        while True:
            try:
                with driver_lock:
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

                            # 2. Parse Contract Results (Win / Loss Detection + Halt Trigger)
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
    """Returns ONLY the main execution Buy button."""
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
with driver_lock:
    for _el, _label, _enabled in list_purchase_buttons(driver):
        print(f"    - {_label} | {'enabled' if _enabled else 'DISABLED'}")

# Start background performance log tick & contract sniffer
start_performance_log_listener(driver)

# --- MONITORING LOOP ---
print("\n[ LIVE SPOT & 10-DIGIT PERCENTAGE TRACKER WITH LOSS HALT ]")
print(f"Condition 1: Digits 8 and 9 must ALL be STRICTLY BELOW {ENTRY_THRESHOLD}% simultaneously")
print(f"Condition 2: Second-to-last quote digit must be between {PREV_DIGIT_MIN} and {PREV_DIGIT_MAX}")
print(f"Condition 3: Difference between 1st least and 2nd least appearing digits must be > {LEAST_PCT_DIFF_MIN}%")
print(f"Condition 4: CHOP must be between {CHOP_MIN} and {CHOP_MAX} (chart tab checked every {int(TV_CHECK_INTERVAL)}s)")
if USE_CCI:
    print(f"Condition 5: CCI must be between {CCI_MIN} and {CCI_MAX} (chart tab checked every {int(TV_CHECK_INTERVAL)}s)")
if USE_ADX:
    print(f"Condition 6: ADX must be between {ADX_MIN} and {ADX_MAX} (chart tab checked every {int(TV_CHECK_INTERVAL)}s)")
print("=" * 120)

last_trade_time = 0
last_processed_count = 0

try:
    while True:
        current_count = tick_state["count"]

        if current_count > last_processed_count:
            last_processed_count = current_count

            with driver_lock:
                stats, status = extract_all_digit_percentages(driver)
            spot_price = tick_state["quote"]
            last_digit = tick_state["digit"]
            prev_digit = tick_state["prev_digit"]

            refresh_indicators()
            current_chop = tv_state["chop"]
            current_cci = tv_state["cci"]
            current_adx = tv_state["adx"]

            if stats:
                spot_str = f"Spot: {spot_price} (Last: {last_digit}, Prev/2nd-last: {prev_digit})" if spot_price else "Spot: Waiting..."

                sorted_digits_asc = sorted(stats.keys(), key=lambda d: stats[d])
                least_digit = sorted_digits_asc[0]
                second_least_digit = sorted_digits_asc[1]

                sorted_digits_desc = sorted(stats.keys(), key=lambda d: stats[d], reverse=True)
                most_digit = sorted_digits_desc[0]

                digit_parts = []
                for d in range(10):
                    tag = ""
                    if d == most_digit:
                        tag = " [L]"
                    elif d == least_digit:
                        tag = " [S]"
                    digit_parts.append(f"[{d}]: {stats[d]:>4.1f}%{tag}")
                digits_output = " | ".join(digit_parts)

                required_digits = [8, 9]
                all_below_threshold = all(stats[d] < ENTRY_THRESHOLD for d in required_digits)
                entry_str = f"Digits 8, 9 < {ENTRY_THRESHOLD}%: Active" if all_below_threshold else f"Digits 8, 9 < {ENTRY_THRESHOLD}%: Inactive"

                diff_least_val = stats[second_least_digit] - stats[least_digit]
                rule_least_diff_met = diff_least_val > LEAST_PCT_DIFF_MIN
                diff_str = f"Least Diff (>{LEAST_PCT_DIFF_MIN}%): {diff_least_val:.1f}% ({'Active' if rule_least_diff_met else 'Inactive'})"

                chop_ok = (current_chop is not None) and (CHOP_MIN <= current_chop <= CHOP_MAX)
                chop_str = f"CHOP: {current_chop} ({'Active' if chop_ok else 'Inactive'})"

                if USE_CCI:
                    cci_ok = (current_cci is not None) and (CCI_MIN <= current_cci <= CCI_MAX)
                    cci_str = f"CCI: {current_cci} ({'Active' if cci_ok else 'Inactive'})"
                else:
                    cci_ok = True
                    cci_str = "CCI: off"

                # ADX Rule Check - must be inside ADX_MIN..ADX_MAX
                if USE_ADX:
                    adx_ok = (current_adx is not None) and (ADX_MIN <= current_adx <= ADX_MAX)
                    adx_str = f"ADX: {current_adx} ({'Active' if adx_ok else 'Inactive'})"
                else:
                    adx_ok = True
                    adx_str = "ADX: off"

                indicators_ok = chop_ok and cci_ok and adx_ok

                strategy_triggered = rule_least_diff_met and indicators_ok
                spot_matches_entry = (last_digit is not None) and all_below_threshold and (last_digit in TARGET_ENTRY_DIGITS)
                prev_ok = (prev_digit is not None) and (PREV_DIGIT_MIN <= prev_digit <= PREV_DIGIT_MAX)

                current_time = time.time()
                score_summary = f"[Wins: {contract_state['total_wins']} | Losses: {contract_state['total_losses']} | Net P/L: {contract_state['net_profit']:.2f}]"

                # Check if the loss lockout is currently active
                in_loss_lockout = current_time < contract_state["loss_cooldown_until"]

                if in_loss_lockout:
                    halt_remaining = int(contract_state["loss_cooldown_until"] - current_time) + 1
                    signal_msg = f"⏳ LOSS HALT ACTIVE! Paused for {halt_remaining}s more | {score_summary}"
                elif strategy_triggered and spot_matches_entry and prev_ok:
                    if (current_time - last_trade_time) > COOLDOWN_SECONDS:
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! Spot last digit ({last_digit}) matched Entry | {score_summary}"
                        if AUTO_TRADE:
                            with driver_lock:
                                ok, detail = execute_trade_click(driver)
                            if ok:
                                print(f"\n[✓] {spot_str} | {signal_msg} -> ORDER EXECUTED! Clicked: {detail}")
                                last_trade_time = current_time
                            else:
                                print(f"\n[!] {spot_str} | {signal_msg} -> Click failed: {detail}")
                        else:
                            print(f"\n[DEMO MODE] {spot_str} | {signal_msg} -> Set AUTO_TRADE = True.")
                            last_trade_time = current_time
                    else:
                        remaining = int(COOLDOWN_SECONDS - (current_time - last_trade_time)) + 1
                        signal_msg = f"🔥 FULL EXECUTION TRIGGER! (Cooldown active: {remaining}s remaining) | {score_summary}"
                elif strategy_triggered and (last_digit in TARGET_ENTRY_DIGITS) and not all_below_threshold:
                    signal_msg = f"Entry digit {last_digit} hit but skipped (Digits 8, 9 must all be < {ENTRY_THRESHOLD}%) | {score_summary}"
                elif strategy_triggered and spot_matches_entry:
                    signal_msg = f"Entry digit {last_digit} hit but 2nd-to-last digit is {prev_digit} (needs {PREV_DIGIT_MIN}-{PREV_DIGIT_MAX}) - skipped | {score_summary}"
                elif rule_least_diff_met and not indicators_ok:
                    reasons = []
                    if not chop_ok:
                        if current_chop is None:
                            reasons.append("No CHOP value read yet")
                        else:
                            reasons.append(f"CHOP out of range ({current_chop} not in {CHOP_MIN} to {CHOP_MAX})")
                    if not cci_ok:
                        if current_cci is None:
                            reasons.append("No CCI value read yet")
                        else:
                            reasons.append(f"CCI out of range ({current_cci} not in {CCI_MIN} to {CCI_MAX})")
                    if not adx_ok:
                        if current_adx is None:
                            reasons.append("No ADX value read yet")
                        else:
                            reasons.append(f"ADX out of range ({current_adx} not in {ADX_MIN} to {ADX_MAX})")
                    signal_msg = f"Waiting... {'; '.join(reasons)} | {score_summary}"
                elif strategy_triggered:
                    signal_msg = f"Strategy Ready (Least2 Diff: {diff_least_val:.1f}%)... Waiting for Digits 8, 9 < {ENTRY_THRESHOLD}% & Spot Match | {score_summary}"
                else:
                    signal_msg = f"Waiting... (Least Diff >{LEAST_PCT_DIFF_MIN}%: {rule_least_diff_met}, CHOP OK: {chop_ok}, CCI OK: {cci_ok}, ADX OK: {adx_ok}) | {score_summary}"

                print(f"{spot_str}  ||  {digits_output}  ||  {entry_str}  ||  {diff_str}  ||  {chop_str}  ||  {cci_str}  ||  {adx_str}  ||  {signal_msg}")
            else:
                print(f"[!] {status}")

        time.sleep(LOOP_DELAY)

except KeyboardInterrupt:
    print(f"\n\n[!] Monitoring stopped by user. Final Stats -> Wins: {contract_state['total_wins']}, Losses: {contract_state['total_losses']}, Net P/L: {contract_state['net_profit']:.2f}")
