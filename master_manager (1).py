import json
import threading
import time
import os
import sys
import subprocess
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# --- GITHUB CONFIGURATION ---
GITHUB_OVER_2_URL = "https://raw.githubusercontent.com/donovanbaraka25-sys/Backup-just-Incase-/refs/heads/main/over2.py"
GITHUB_UNDER_7_URL = "https://raw.githubusercontent.com/donovanbaraka25-sys/Backup-just-Incase-/refs/heads/main/Back-up.py"

# Strategies are saved in your user folder (always writable), NOT in Program Files.
STRATEGY_DIR = os.path.join(os.path.expanduser("~"), "deriv_strategies")
os.makedirs(STRATEGY_DIR, exist_ok=True)

# One file per strategy, so a file that is running/open is never overwritten.
STRATEGY_FILES = {
    "over_2": os.path.join(STRATEGY_DIR, "strategy_over2.py"),
    "under_7": os.path.join(STRATEGY_DIR, "strategy_under7.py"),
}

# Strategy Lock Duration: 5 minutes (300 seconds) before re-checking regime
STRATEGY_LOCK_DURATION = 300
# Wait this long after a failed download before trying again
DOWNLOAD_RETRY_DELAY = 30

# Connect to active Chrome instance with performance logging enabled
chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

try:
    driver = webdriver.Chrome(options=chrome_options)
    print("[✓] Connected to live Chrome session.")
except Exception as e:
    print(f"[✗] Connection error: {e}")
    input("\nPress Enter to exit...")
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
    input("\nPress Enter to exit...")
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


# --- PROVEN DOM PERCENTAGE EXTRACTOR ---
def extract_all_digit_percentages(driver):
    """Pulls all 10 digit percentages from the DTrader DOM stats table."""
    js_code = r"""
    let allNodes = Array.from(document.querySelectorAll('*'));
    let pctNodes = allNodes.filter(el => {
        let text = el.innerText ? el.innerText.trim() : '';
        return /^\d+(\.\d+)?%$/.test(text) && el.children.length === 0;
    });
    return pctNodes.map(el => el.innerText.trim());
    """
    try:
        raw_results = driver.execute_script(js_code)
        if not raw_results:
            return None, "No percentage nodes found on page."
        cleaned = [float(val.replace("%", "").strip()) for val in raw_results]
        if len(cleaned) >= 10:
            digit_stats = cleaned[-10:]
            return {d: digit_stats[d] for d in range(10)}, "ok"
        return None, f"Found only {len(cleaned)} percentage nodes (need 10)."
    except Exception as e:
        return None, f"JS error: {str(e)[:100]}"


def download_strategy_from_github(url, local_filename):
    """Downloads the strategy file from GitHub. Returns the saved file path, or None on failure."""
    try:
        print(f"[i] Downloading strategy from: {url}")
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            print(f"[✗] Failed to download. HTTP Status Code: {response.status_code}")
            return None

        # Write to a temp file, then atomically swap it in.
        # os.replace overwrites an existing file in one step (os.remove + os.rename
        # is what was failing with Errno 13).
        temp_filename = local_filename + ".tmp"
        with open(temp_filename, "w", encoding="utf-8") as f:
            f.write(response.text)

        try:
            os.replace(temp_filename, local_filename)
        except PermissionError:
            # Target is locked (running/open elsewhere). Save under a new name instead.
            fallback = local_filename.replace(".py", f"_{int(time.time())}.py")
            os.replace(temp_filename, fallback)
            print(f"[!] {local_filename} is locked. Saved as {fallback} instead.")
            return fallback

        print(f"[✓] Strategy saved to {local_filename}")
        return local_filename
    except Exception as e:
        print(f"[✗] Exception during download: {e}")
        return None


def set_dtrader_ui_state(driver, target_contract, target_digit):
    """Automatically clicks the contract type (over/under) and prediction digit on DTrader UI."""
    js_code = r"""
    return (function(targetContract, targetDigit) {
        let allEls = Array.from(document.querySelectorAll('button, div, span, label'));

        // 1. Set Contract Type if needed
        let contractSet = false;
        for (let el of allEls) {
            let txt = el.innerText ? el.innerText.trim().toLowerCase() : '';
            if (txt === targetContract && el.children.length <= 2) {
                let bg = window.getComputedStyle(el).backgroundColor;
                let isSelected = bg.includes('255, 255, 255') || bg.includes('rgb(255') || el.getAttribute('aria-selected') === 'true' || el.className.includes('active');
                if (!isSelected) {
                    el.click();
                }
                contractSet = true;
                break;
            }
        }

        // 2. Set Prediction Digit if needed
        let digitSet = false;
        let candidates = allEls.filter(el => el.children.length <= 1 && el.innerText && el.innerText.trim() === String(targetDigit));
        for (let el of candidates) {
            let parent = el.parentElement;
            let bg = window.getComputedStyle(el).backgroundColor;
            let isPressed = el.getAttribute('aria-pressed') === 'true' || (parent && parent.getAttribute('aria-pressed') === 'true');
            let isDarkBg = bg && !bg.includes('rgba(0, 0, 0, 0)') && !bg.includes('transparent') && !bg.includes('255, 255, 255');

            if (!isPressed && !isDarkBg) {
                el.click();
            }
            digitSet = true;
            break;
        }

        return { contract_set: contractSet, digit_set: digitSet };
    })(arguments[0], arguments[1]);
    """
    try:
        return driver.execute_script(js_code, target_contract, target_digit)
    except Exception as e:
        return {"error": str(e)}


running_process = None  # the currently running strategy subprocess


def stop_running_strategy():
    """Stops the strategy that is currently running (if any)."""
    global running_process
    if running_process and running_process.poll() is None:
        print("[i] Stopping previous strategy...")
        running_process.terminate()
        try:
            running_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            running_process.kill()
    running_process = None


def launch_strategy(script_path):
    """Runs the downloaded strategy in its own console window. Returns True if it started."""
    global running_process
    stop_running_strategy()
    try:
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        running_process = subprocess.Popen(
            [sys.executable, script_path],
            cwd=os.path.dirname(script_path),
            creationflags=flags,
        )
        time.sleep(3)  # give it a moment, then check it didn't crash immediately
        if running_process.poll() is not None:
            print(f"[✗] Strategy exited immediately (code {running_process.returncode}). Check its console window / errors.")
            running_process = None
            return False
        print(f"[✓] Strategy running (PID {running_process.pid}): {script_path}")
        return True
    except Exception as e:
        print(f"[✗] Could not launch strategy: {e}")
        running_process = None
        return False


def activate_strategy(key, label, ui_contract, ui_digit, url):
    """Sets the UI, downloads the script, launches it, and returns True if it worked."""
    print(f"[+] Regime matched: {label}. Updating UI ({ui_contract.title()}, Digit {ui_digit}) & fetching script...")
    ui_result = set_dtrader_ui_state(driver, ui_contract, ui_digit)
    print(f"[i] UI result: {ui_result}")  # shows JS errors / missing buttons
    saved_path = download_strategy_from_github(url, STRATEGY_FILES[key])
    if not saved_path:
        return False
    return launch_strategy(saved_path)


# Start background performance log tick sniffer
start_performance_log_listener(driver)

# --- MASTER MANAGER MONITORING LOOP ---
print("\n[ MASTER MANAGER INITIALIZED ]")
print(f"Strategies are saved in: {STRATEGY_DIR}")
print("Analyzing market regime, managing GitHub scripts & enforcing 5-minute locks...")
print("=" * 130)

current_active_strategy = None
strategy_lock_until = 0
next_download_attempt = 0  # backoff after a failed download

try:
    while True:
        current_time = time.time()

        # If a strategy is locked in, bypass regime checking for the 5-minute duration
        if current_active_strategy and current_time < strategy_lock_until:
            remaining_lock = int(strategy_lock_until - current_time)
            m, s = divmod(remaining_lock, 60)
            print(f"[LOCK ACTIVE] Strategy '{current_active_strategy}' running. Time remaining: {m:02d}m {s:02d}s")
            time.sleep(5)
            continue

        # Fetch digit percentages from stats table
        stats, status = extract_all_digit_percentages(driver)
        spot_price = tick_state["quote"]
        last_digit = tick_state["digit"]
        prev_digit = tick_state["prev_digit"]

        if stats:
            # 1. Calculate group sums for Digits 0-4 and 5-9
            sum_0_to_4 = sum(stats[d] for d in range(0, 5))
            sum_5_to_9 = sum(stats[d] for d in range(5, 10))

            # 2. Sort digits by percentage descending to find 1st and 2nd most appearing digits
            sorted_digits = sorted(stats.keys(), key=lambda d: stats[d], reverse=True)
            most_digit = sorted_digits[0]
            second_digit = sorted_digits[1]

            # 3. Determine Market Regime
            detected_regime = "MIXED"
            if most_digit in range(0, 5) and second_digit in range(0, 5):
                detected_regime = "UNDER_7"
            elif most_digit in range(5, 10) and second_digit in range(5, 10):
                detected_regime = "OVER_2"

            digit_parts = []
            for d in range(10):
                tag = ""
                if d == most_digit:
                    tag = " [1st]"
                elif d == second_digit:
                    tag = " [2nd]"
                digit_parts.append(f"[{d}]: {stats[d]:>4.1f}%{tag}")
            digits_output = " | ".join(digit_parts)

            print(f"Regime: {detected_regime:<8} | Top2: [{most_digit}, {second_digit}]  ||  Sum(0-4): {sum_0_to_4:5.1f}% | Sum(5-9): {sum_5_to_9:5.1f}%  ||  {digits_output}")

            # 4. Strategy Switcher & UI Auto-Configurator
            wanted = None
            if detected_regime == "OVER_2":
                wanted = ("over_2", "Over 2", "over", 2, GITHUB_OVER_2_URL)
            elif detected_regime == "UNDER_7":
                wanted = ("under_7", "Under 7", "under", 7, GITHUB_UNDER_7_URL)

            if wanted and current_active_strategy != wanted[0] and current_time >= next_download_attempt:
                key, label, ui_contract, ui_digit, url = wanted
                if activate_strategy(key, label, ui_contract, ui_digit, url):
                    current_active_strategy = key
                    strategy_lock_until = time.time() + STRATEGY_LOCK_DURATION
                    next_download_attempt = 0
                    print(f"[✓] {label} strategy started. Locked in for 5 minutes.")
                else:
                    next_download_attempt = time.time() + DOWNLOAD_RETRY_DELAY
                    print(f"[!] Download/launch failed. Retrying in {DOWNLOAD_RETRY_DELAY}s.")
        else:
            print(f"[!] Status: {status}")

        time.sleep(1)

except KeyboardInterrupt:
    print("\n\n[!] Master Manager stopped by user.")
    stop_running_strategy()
except Exception as e:
    print(f"\n\n[✗] Critical Error: {e}")
    stop_running_strategy()
    input("\nPress Enter to exit...")
