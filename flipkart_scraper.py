"""flipkart_scraper.py

Self-contained Flipkart price scraper that drives a real Chrome browser.

Drop this single file into any app: create one FlipkartScraper, call
.scrape(pid) as many times as you like (the browser is launched once and
reused), then .close(). It reads, per product:

    name : product title
    fsp  : Flipkart selling price (the big price shown on the page)
    nep  : lowest price after bank / exchange offers
    mrp  : maximum retail price

Requirements:
    pip install selenium
    pip install selenium-stealth      # optional, reduces bot detection

Driver resolution:
    A local "chromedriver.exe" (matching your installed Chrome) placed next to
    this file is used if present; otherwise Selenium Manager (built into
    Selenium 4.6+) auto-downloads a matching driver. No webdriver-manager.

Use as a module:
    from flipkart_scraper import FlipkartScraper

    with FlipkartScraper(headless=True) as scraper:
        data = scraper.scrape("MOBHDVFKSF3YZQNK")
        print(data["fsp"], data["nep"])

Use from the command line:
    python flipkart_scraper.py MOBHDVFKSF3YZQNK MOBHMGHHWHNH99DX --delay 10
"""

from __future__ import annotations

import os
import re
import json
import time
import threading
import subprocess
import argparse
from cookie_manager import load_cookies_from_mongo
from datetime import datetime

# Optional SSL cert fix for MSYS2 / MinGW Python; harmless on normal CPython.
try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except Exception:
    pass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException

# selenium-stealth is optional. If installed it reduces bot detection; if not,
# the scraper still works (we hit slowly, so blocking isn't a concern).
try:
    from selenium_stealth import stealth
except Exception:
    stealth = None

# Import config to check if bank offers extraction is enabled
try:
    from config import EXTRACT_BANK_OFFERS, BANK_OFFERS_VERBOSE
except ImportError:
    EXTRACT_BANK_OFFERS = False
    BANK_OFFERS_VERBOSE = False

# Import bank offers extractor module if enabled
if EXTRACT_BANK_OFFERS:
    try:
        from bank_offers_extractor import extract_bank_offers
    except ImportError:
        extract_bank_offers = None
        EXTRACT_BANK_OFFERS = False
else:
    extract_bank_offers = None


BASE_URL = "https://www.flipkart.com/product/p/itm?pid={pid}&lid={lid}"

# Platform-aware chromedriver path
import platform
import glob

_chromedriver_name = "chromedriver.exe" if platform.system() == "Windows" else "chromedriver"
DEFAULT_CHROMEDRIVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), _chromedriver_name
)


def _find_chrome_binary() -> str | None:
    """Return the actual Chrome/Chromium binary installed on the machine."""
    candidates = [
        os.getenv("GOOGLE_CHROME_BIN"),
        os.getenv("CHROME_BIN"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _find_chromedriver() -> str | None:
    """Prefer a stable, locally installed driver before Selenium Manager."""
    explicit = os.getenv("CHROMEDRIVER_PATH")
    if explicit and os.path.exists(explicit):
        return explicit

    repo_local = DEFAULT_CHROMEDRIVER
    if os.path.exists(repo_local):
        return repo_local

    system_candidates = [
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
        "/usr/bin/chromium-driver",
        "/snap/bin/chromium.chromedriver",
    ]
    for candidate in system_candidates:
        if os.path.exists(candidate):
            return candidate

    cache_glob = os.path.expanduser("~/.cache/selenium/chromedriver/linux64/*/chromedriver")
    matches = sorted(glob.glob(cache_glob))
    if matches:
        return matches[-1]

    return None

# Try multiple cookie files in order: client → vasu → fallback
COOKIES_FILES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_cookies.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vasu_cookies.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "flipkart_cookies.json"),
]

# Hardcoded cookies as fallback (for Railway/Docker environments)
HARDCODED_COOKIES = [{"domain":"www.flipkart.com","httpOnly":False,"name":"fonts-loaded","path":"/","sameSite":"Lax","secure":False,"value":"en_loaded"},{"domain":".flipkart.com","httpOnly":False,"name":"Network-Type","path":"/","sameSite":"Lax","secure":False,"value":"4g"},{"domain":".flipkart.com","expiry":1814087839,"httpOnly":False,"name":"vw","path":"/","sameSite":"Lax","secure":False,"value":"1427"},{"domain":".flipkart.com","expiry":1798103835,"httpOnly":True,"name":"ULSN","path":"/","sameSite":"Lax","secure":True,"value":"eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb29raWUiLCJhdWQiOiJmbGlwa2FydCIsImlzcyI6ImF1dGguZmxpcGthcnQuY29tIiwiY2xhaW1zIjp7ImdlbiI6IjEiLCJ1bmlxdWVJZCI6IlVVSTI2MDYyNzE0NDcxNjM2Nlg4Q0I5OUoiLCJma0RldiI6bnVsbH0sImV4cCI6MTc5ODMzMTgzNiwiaWF0IjoxNzgyNTUxODM2LCJqdGkiOiI3ZjFlN2ZjMC0wNTI0LTRlNDgtYmFkNi05NzMzZDA3YWFhODUifQ.p6P0R5jBOaCjKjyndxGDq1ZOeFdg9mfVEdOjLcW7kH8"},{"domain":".flipkart.com","expiry":1798103835,"httpOnly":True,"name":"ud","path":"/","sameSite":"None","secure":True,"value":"8.6uH41AnRCPNLlFt7n-PoRhTd8N2dPG8wYJZtL5RziDULLaudJbOGXwRL42ftRd5dYoZCy-mdoqx5mjmJDCfYNPZXSGHWlJoAFEbBRZyMGS5U9hQeycGP1bE1V2gNXGkRAWaYbVPwBCPCsbTGUWoH3PzWaxZ7nquUMXRrJE-ZoI6n_coAY900gsoBCrGL_Bw9"},{"domain":".flipkart.com","expiry":1798103842,"httpOnly":True,"name":"S","path":"/","sameSite":"None","secure":True,"value":"d1t10TT8SPyE/PB0/P0k/Pz8GP+TXKQjSoACuHluyNeZjpvwJ4icUPxconIbI7aNB+oUGxxvQ58K4tY41H7pMc2/Tww=="},{"domain":".flipkart.com","expiry":1798330324,"httpOnly":True,"name":"at","path":"/","sameSite":"Lax","secure":False,"value":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IjNhNzdlZTgxLTRjNWYtNGU5Ni04ZmRlLWM3YWMyYjVlOTA1NSJ9.eyJleHAiOjE3ODI1NTM2MzYsImlhdCI6MTc4MjU1MTgzNiwiaXNzIjoia2V2bGFyIiwianRpIjoiODgxNmY3MmItYjkzNy00MTViLWE2OTMtZWY3ZWVjYzM4OTM2IiwidHlwZSI6IkFUIiwiZElkIjoiVEkxNzgyNTUxNzg0MTY4MDAwODk2MDA0MjYwODEwNjQ1NzQ5MTM0MjczODM3NjcxOTg1NTEyODU4OTY2ODIyMzA5NDQiLCJiSWQiOiJQNlNHR00iLCJrZXZJZCI6IlZJQ0E5QkUwNzgzRDc5NEM1Mzg2ODdCQzVGMjFDRUZBOEYiLCJ0SWQiOiJtYXBpIiwiZWFJZCI6IldYVUc5UWZIQmZuUy1yRG55eWZ2NlV1N2htUVBtMW53SFA4X29aZGtqaHFNQXhMdW51ZnQ2dz09IiwidnMiOiJMSSIsInoiOiJDSCIsIm0iOnRydWUsImdlbiI6MywiZG9tIjoiRkxJUEtBUlQifQ.LJ3xbuVRTnfjsuWw_oyzJFEtE8psTwpSj9mTAlMoRUY"},{"domain":".flipkart.com","expiry":1798330324,"httpOnly":True,"name":"rt","path":"/","sameSite":"Lax","secure":False,"value":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IjhlM2ZhMGE3LTJmZDMtNGNiMi05MWRjLTZlNTMxOGU1YTkxZiJ9.eyJleHAiOjE3OTgzNjMwMzYsImlhdCI6MTc4MjU1MTgzNiwiaXNzIjoia2V2bGFyIiwianRpIjoiYjA3YWE1YWEtY2Q2My00Y2VmLWEzZWYtZTlkZTE3YjU4YzQ1IiwidHlwZSI6IlJUIiwiZElkIjoiVEkxNzgyNTUxNzg0MTY4MDAwODk2MDA0MjYwODEwNjQ1NzQ5MTM0MjczODM3NjcxOTg1NTEyODU4OTY2ODIyMzA5NDQiLCJiSWQiOiJQNlNHR00iLCJrZXZJZCI6IlZJQ0E5QkUwNzgzRDc5NEM1Mzg2ODdCQzVGMjFDRUZBOEYiLCJ0SWQiOiJtYXBpIiwibSI6eyJ0eXBlIjoibiJ9LCJ2IjoiTFRNMUFPIiwiZG9tIjoiRkxJUEtBUlQifQ.nwuuNwcl0MQjMXfG0u-ByqUQpuFkUniqXHZutMRGXPQ"},{"domain":"www.flipkart.com","httpOnly":False,"name":"isH2EnabledBandwidth","path":"/","sameSite":"Lax","secure":False,"value":"false"},{"domain":".flipkart.com","expiry":1798330324,"httpOnly":True,"name":"SN","path":"/","sameSite":"Lax","secure":False,"value":"VICA9BE0783D794C538687BC5F21CEFA8F.TOKD785D7AF45BB4D739E2742ED88A6EB35.1782551848941.LI"},{"domain":"www.flipkart.com","httpOnly":False,"name":"h2NetworkBandwidth","path":"/","sameSite":"Lax","secure":False,"value":"9"},{"domain":".flipkart.com","expiry":1814087839,"httpOnly":False,"name":"dpr","path":"/","sameSite":"Lax","secure":False,"value":"1.25"},{"domain":".flipkart.com","expiry":1814087839,"httpOnly":False,"name":"vh","path":"/","sameSite":"Lax","secure":False,"value":"788"},{"domain":".flipkart.com","expiry":1814087848,"httpOnly":False,"name":"T","path":"/","sameSite":"None","secure":True,"value":"TI178255178416800089600426081064574913427383767198551285896682230944"},{"domain":".flipkart.com","expiry":1798330324,"httpOnly":True,"name":"K-ACTION","path":"/","sameSite":"Lax","secure":False,"value":"null"},{"domain":".flipkart.com","expiry":1817111857,"httpOnly":False,"name":"AMCV_17EB401053DAF4840A490D4C%40AdobeOrg","path":"/","sameSite":"Lax","secure":False,"value":"-227196251%7CMCIDTS%7C20632%7CMCMID%7C85114758887347900242505641865757520102%7CMCAID%7CNONE"},{"domain":".flipkart.com","expiry":1798103788,"httpOnly":True,"name":"vd","path":"/","sameSite":"None","secure":True,"value":"VICA9BE0783D794C538687BC5F21CEFA8F-1782551789861-1.1782551789.1782551789.157378486"}]

# Flipkart obfuscates class names and changes them often, so we try several.
FSP_SELECTORS = [
    "div.Nx9bqj",
    "div._30jeq3",
    "div.CEmiEU",
    "[class*='finalPrice']",
    "[class*='selling']",
]
NEP_SELECTORS = [
    "div.UOCQB1",
    "div.HO6D9a",
    "div._3I9_wc",
    "[class*='nepPrice']",
    "[class*='lowest']",
]


class FlipkartScraper:
    """A reusable, browser-backed Flipkart scraper.

    The Chrome instance is launched lazily on the first scrape and reused for
    every subsequent call, so cookies/session carry over and you only pay the
    startup cost once.
    """

    def __init__(
        self,
        headless: bool = False,
        chromedriver_path: str | None = None,
        page_load_timeout: int = 25,
        render_wait: float = 0.5,
        hard_timeout: float = 55.0,
    ):
        self.headless = headless
        self.chrome_binary = _find_chrome_binary()
        self.chromedriver_path = chromedriver_path or _find_chromedriver() or DEFAULT_CHROMEDRIVER
        self.page_load_timeout = page_load_timeout
        self.render_wait = render_wait
        # Absolute ceiling for a single scrape attempt. page_load_timeout only
        # caps driver.get(); reads like page_source/find_element have no
        # timeout and can block forever if the renderer/network dies. A
        # watchdog enforces this hard limit and force-kills Chrome if hit.
        self.hard_timeout = hard_timeout
        self._driver: webdriver.Chrome | None = None
        self._cookies_loaded = False
        self.account_name = None  # Store logged-in account name (display only, may be a guess)
        self.login_confirmed = False  # True only when the real Flipkart "Hi, <Name>" greeting was found

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _ensure_driver(self) -> webdriver.Chrome:
        if self._driver is not None:
            return self._driver

        opts = Options()
        if self.chrome_binary:
            opts.binary_location = self.chrome_binary
        # "none" returns from driver.get() as soon as the initial HTML arrives,
        # without waiting for images/sub-resources that Flipkart often stalls
        # on (which otherwise wedges the renderer). The price JSON ("ppd") is
        # in that initial HTML, so we just poll page_source for it afterwards.
        opts.page_load_strategy = "none"
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1440,900")
        opts.add_argument("--lang=en-IN")
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        if os.path.exists(self.chromedriver_path):
            driver = webdriver.Chrome(
                service=Service(self.chromedriver_path), options=opts
            )
        else:
            driver = webdriver.Chrome(options=opts)

        if getattr(driver, "service", None) is not None:
            try:
                if getattr(driver.service, "path", None):
                    print(f"[chrome] using driver={driver.service.path} binary={self.chrome_binary or opts.binary_location}", flush=True)
            except Exception:
                pass

        if stealth is not None:
            stealth(
                driver,
                languages=["en-IN", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )

        driver.set_page_load_timeout(self.page_load_timeout)
        
        # Disable cache entirely to force fresh prices
        driver.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled': True})
        
        self._driver = driver
        
        # Load cookies on first driver creation
        self._load_cookies(driver)
        
        return driver

    def _load_cookies(self, driver: webdriver.Chrome) -> None:
        """Load saved Flipkart cookies from MongoDB (with local fallback)."""
        if self._cookies_loaded:
            return
        
        cookies = None
        cookie_source = None
        
        # Priority 1: Try MongoDB first
        try:
            mongo_cookies = load_cookies_from_mongo("primary")  # Load primary account
            if mongo_cookies:
                cookies = mongo_cookies
                cookie_source = "MongoDB (primary)"
        except Exception as e:
            print(f"MongoDB cookie load failed: {e}")
        
        # Priority 2: Try environment variable (for Railway/Docker)
        if not cookies and os.getenv("FLIPKART_COOKIES"):
            try:
                cookies = json.loads(os.getenv("FLIPKART_COOKIES"))
                cookie_source = "env"
            except Exception as e:
                print(f"Failed to parse FLIPKART_COOKIES env var: {e}")
        
        # Priority 3: Try local cookie files
        if not cookies:
            cookies_file = None
            for cookie_path in COOKIES_FILES:
                if os.path.exists(cookie_path):
                    cookies_file = cookie_path
                    break
            
            if cookies_file:
                with open(cookies_file, "r") as f:
                    cookies = json.load(f)
                cookie_source = os.path.basename(cookies_file).replace('_cookies.json', '').replace('cookies.json', 'flipkart')
        
        # Priority 4: Use hardcoded cookies as final fallback
        if not cookies:
            cookies = HARDCODED_COOKIES
            cookie_source = "hardcoded"
        
        if not cookies:
            return
        
        try:
            # Navigate to Flipkart first (cookies need matching domain)
            driver.get("https://www.flipkart.com")
            time.sleep(1)
            
            # Delete all existing cookies
            driver.delete_all_cookies()
            
            loaded_count = 0
            for cookie in cookies:
                try:
                    # Remove fields that Selenium doesn't accept, but keep sameSite
                    cookie.pop("storeId", None)
                    # Convert sameSite to format Selenium expects
                    if "sameSite" in cookie:
                        same_site = cookie.pop("sameSite")
                        # Selenium expects "Strict", "Lax", or "None"
                        if same_site:
                            cookie["sameSite"] = same_site
                    driver.add_cookie(cookie)
                    loaded_count += 1
                except Exception as e:
                    # Skip invalid cookies
                    pass
            
            # Refresh to activate cookies
            driver.refresh()
            time.sleep(2)  # Wait for page to load with cookies
            
            # Extract account name from the page
            account_name = None
            login_confirmed = False  # Only Method 1 (the real greeting) counts as proof of login
            try:
                # Method 1: Try to find account name in common locations
                # Look for "Hi, [Name]" or similar patterns
                try:
                    # Try finding the account dropdown or link
                    elements = driver.find_elements("xpath", "//*[contains(text(), 'Hi,') or contains(text(), 'Hello,')]")
                    for elem in elements:
                        text = elem.text.strip()
                        if text.startswith(('Hi,', 'Hello,')):
                            name = text.replace('Hi,', '').replace('Hello,', '').strip()
                            if name and len(name) < 20:
                                account_name = name
                                login_confirmed = True
                                break
                except:
                    pass
                
                # Method 2: Check cookies for user info
                if not account_name:
                    try:
                        all_cookies = driver.get_cookies()
                        for cookie in all_cookies:
                            if 'SN' in cookie.get('name', '') or 'user' in cookie.get('name', '').lower():
                                value = cookie.get('value', '')
                                # Try to decode if it looks like it contains a name
                                if value and 2 < len(value) < 30 and not value.startswith('_'):
                                    # Simple heuristic: if it's mostly alphanumeric with spaces, might be a name
                                    clean = value.replace('%20', ' ').replace('+', ' ')
                                    if clean.replace(' ', '').isalpha() and len(clean) < 20:
                                        account_name = clean
                                        break
                    except:
                        pass
                
                # Method 3: Fallback - look in page text for patterns
                if not account_name:
                    body_text = driver.find_element("tag name", "body").text
                    lines = [line.strip() for line in body_text.split('\n') if line.strip()]
                    
                    for line in lines[:100]:  # Check first 100 lines
                        # Look for short text (2-20 chars) that's alphabetic
                        if 2 <= len(line) <= 20 and line.replace(' ', '').isalpha():
                            # Skip common UI words (but NOT "Account" - that's a valid name)
                            skip_words = ['Login', 'Cart', 'Search', 'Grocery', 'Mobile', 'Fashion', 
                                         'Electronics', 'Home', 'Furniture', 'Appliances', 'Travel', 
                                         'Beauty', 'Toys', 'Two', 'More', 'Offer', 'Top', 'Become',
                                         'Seller', 'Gift', 'Help', 'My', 'Logout', 'Orders',
                                         'Wishlist', 'Rewards', 'Cards', 'Super', 'Coin', 'Plus',
                                         'Search Icon', 'Icon', 'for', 'and', 'the', 'For', 'You',
                                         'New', 'Best', 'Shop', 'Now', 'View', 'All', 'See', 'Get',
                                         'Save', 'Buy', 'Add', 'Remove', 'Close', 'Open', 'Menu']
                            if line not in skip_words and not line.isupper() and line.isalpha():
                                account_name = line
                                break
            except Exception:
                pass

            # Method 4: Flipkart's current UI no longer shows a "Hi,"/"Hello,"
            # greeting when logged in - it just shows the plain account name in
            # the top nav, with no "Login" button. The safest signal is a real
            # account name combined with the absence of the login CTA OR the
            # presence of that account name in the visible page text. This avoids
            # false negatives on valid sessions where the old greeting is gone.
            if not login_confirmed:
                try:
                    body_text = driver.find_element("tag name", "body").text
                    top_lines = [line.strip() for line in body_text.split('\n') if line.strip()][:20]
                    body_lower = body_text.lower()
                    account_name_lower = (account_name or '').lower()
                    has_login_cta = any('login' in line.lower() for line in top_lines)
                    has_account_name = account_name_lower and account_name_lower in body_lower
                    if account_name and (not has_login_cta or has_account_name):
                        login_confirmed = True
                except Exception:
                    pass

            # Log the result
            self.login_confirmed = login_confirmed
            if account_name:
                self.account_name = account_name
                confirmed_note = "" if login_confirmed else " (⚠ unconfirmed guess, real greeting not found)"
                print(f"✓ Loaded {loaded_count} cookies from {cookie_source} - Logged in as: {account_name}{confirmed_note}", flush=True)
            else:
                self.account_name = None
                print(f"✓ Loaded {loaded_count} cookies from {cookie_source} - Logged in", flush=True)
            
            self._cookies_loaded = True
        except Exception as e:
            print(f"⚠ Cookie loading failed: {e}", flush=True)
    
    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            self._cookies_loaded = False

    def _force_kill_driver(self) -> None:
        """Hard-kill the chromedriver process tree. Used when a scrape exceeds
        the watchdog deadline: killing chromedriver makes any blocked Selenium
        command return immediately so the wedged worker thread can unwind."""
        drv = self._driver
        self._driver = None
        self._cookies_loaded = False
        if drv is None:
            return
        try:
            proc_pid = drv.service.process.pid
        except Exception:
            proc_pid = None
        if proc_pid is not None:
            try:
                if platform.system() == "Windows":
                    # /T also kills the child Chrome processes so they don't leak.
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc_pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                else:
                    # Linux/macOS (e.g. the Hostinger server): taskkill does not
                    # exist there, so a wedged Chrome used to never be killed.
                    # Kill chromedriver's children (Chrome) first, then chromedriver.
                    subprocess.run(
                        ["pkill", "-9", "-P", str(proc_pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                    try:
                        os.kill(proc_pid, 9)
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            drv.quit()
        except Exception:
            pass

    def __enter__(self) -> "FlipkartScraper":
        self._ensure_driver()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── scraping ──────────────────────────────────────────────────────────
    def scrape(self, pid: str, lid: str = "", _attempts: int = 2) -> dict:
        """Scrape one product. Always returns a dict; on failure the 'error'
        key is set and the price fields are None.

        Each attempt runs under a hard watchdog: if a Selenium call hangs (dead
        renderer / lost network), the deadline fires, chromedriver is killed,
        and a fresh Chrome is launched for the next attempt. This guarantees a
        scrape can never block the caller longer than ~hard_timeout per attempt.
        """
        url = BASE_URL.format(pid=pid, lid=lid) if lid else f"https://www.flipkart.com/product/p/itm?pid={pid}"
        last_html_len = 0
        last_err = None

        for attempt in range(_attempts):
            holder: dict = {}

            def _work():
                try:
                    holder["result"] = self._scrape_once(pid, url)
                except Exception as e:
                    holder["error"] = (
                        str(e).splitlines()[0] if str(e) else type(e).__name__
                    )

            worker = threading.Thread(target=_work, daemon=True)
            worker.start()
            worker.join(self.hard_timeout)

            if worker.is_alive():
                # Wedged — kill chromedriver so the blocked call returns, then
                # retry with a clean browser.
                last_err = f"hard timeout after {self.hard_timeout:.0f}s"
                self._force_kill_driver()
                worker.join(5)
                continue

            if "error" in holder:
                last_err = holder["error"]
                self.close()  # fresh Chrome next attempt
                continue

            result = holder.get("result") or {}
            # CAPTCHA / block is definitive — don't burn the second attempt.
            if result.get("error"):
                return result
            if result.get("fsp") or result.get("nep"):
                return result
            last_html_len = result.get("_html_len", last_html_len)
            # else: empty result — loop reloads the page and retries.

        if last_err:
            return self._error(pid, url, last_err)
        if last_html_len < 5000:
            # Soft error for blocked pages - will retry next cycle
            return {"pid": pid, "url": url, "fsp": None, "nep": None, 
                    "error": "BLOCKED", "name": None, "mrp": None}
        return self._error(pid, url, "No price found after retries")

    def _scrape_once(self, pid: str, url: str) -> dict:
        """One scrape attempt. Returns a success dict (error=None, fsp/nep set),
        a CAPTCHA error dict, or an empty marker dict ({"_empty": True,
        "_html_len": N}). Runs inside the watchdog worker thread."""
        t_start = time.time()
        driver = self._ensure_driver()

        # With page_load_strategy="none", get() returns almost immediately; set
        # a timeout so a stalled connection can't hang us forever.
        try:
            driver.set_page_load_timeout(self.page_load_timeout)
            driver.get(url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        self._dismiss_login_popup(driver)

        # Poll page_source until the price JSON appears (or the page is clearly
        # a captcha/block), up to ~15s.
        prices: dict = {}
        html = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                html = driver.page_source
            except Exception:
                time.sleep(0.5)
                continue
            low = html.lower()
            if "please solve the captcha" in low or "are you a robot" in low:
                return self._error(pid, url, "CAPTCHA detected")
            prices = extract_ppd_prices(html)
            # Keep polling until we have BOTH fsp and nep — breaking on only
            # fsp means we might read the ppd before nepPrice is populated.
            if prices.get("fsp") and prices.get("nep"):
                break
            # If deadline is close and we at least have fsp, accept it
            if prices.get("fsp") and time.time() + 1.5 > deadline:
                break
            time.sleep(0.2)

        # JSON never showed up — settle, then try a DOM scrape.
        if not (prices.get("fsp") or prices.get("nep")):
            time.sleep(self.render_wait)
            try:
                html = driver.page_source
                prices = extract_ppd_prices(html)
            except Exception:
                pass
            if not (prices.get("fsp") or prices.get("nep")):
                try:
                    prices = self._extract_from_dom(driver)
                except Exception:
                    pass
        
        # CRITICAL: Always verify NEP from DOM — DOM "Lowest price for you" is
        # the authoritative value. JSON nepPrice can contain EMI-adjusted prices
        # which are misleading. Always prefer DOM if it finds a value.
        if prices.get("fsp") or prices.get("nep"):
            try:
                dom_prices = self._extract_from_dom(driver)
                if dom_prices.get("nep"):
                    # Always use DOM NEP — it reads "Lowest price for you" label directly
                    prices["nep"] = dom_prices["nep"]
                    prices["source"] = "ppd_json+dom_nep"
            except Exception:
                pass

        if prices.get("fsp") or prices.get("nep"):
            # Extract bank offers; retry once if empty (carousel may not have rendered yet)
            t_offers_start = time.time()
            bank_offers = self._extract_bank_offers(driver, html)
            if not bank_offers:
                time.sleep(1.5)
                bank_offers = self._extract_bank_offers(driver, html)
            t_end = time.time()

            return {
                "timing": {
                    "price_s":  round(t_offers_start - t_start, 1),  # page load + price read
                    "offers_s": round(t_end - t_offers_start, 1),    # bank-offer carousel
                },
                "pid":    pid,
                "name":   self._product_name(driver, html) or pid,
                "fsp":    prices.get("fsp"),
                "nep":    prices.get("nep"),
                "mrp":    prices.get("mrp"),
                "bank_offers": bank_offers,
                "url":    url,
                "source": prices.get("source", "none"),
                "error":  None,
                "raw_html": html,  # Include full HTML for debugging
            }
        return {"_empty": True, "_html_len": len(html)}

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _error(pid: str, url: str, msg: str) -> dict:
        return {
            "pid": pid, "name": pid, "fsp": None, "nep": None,
            "mrp": None, "url": url, "source": "none", "error": msg,
        }

    @staticmethod
    def _dismiss_login_popup(driver: webdriver.Chrome) -> None:
        # One browser round-trip instead of one per <button> on the page
        # (the old loop read .text of every button individually, which is slow).
        try:
            clicked = driver.execute_script(
                """
                const sels = ["button._2KpZ6l._2doB4z", "button._30XB9F",
                              "span._30XB9F", "button[class*='_2doB4z']"];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) { el.click(); return true; }
                }
                const closers = ["\\u2715", "\\u00d7", "x", "X"];
                for (const b of document.querySelectorAll('button')) {
                    if (closers.includes((b.innerText || '').trim())) { b.click(); return true; }
                }
                return false;
                """
            )
            if clicked:
                time.sleep(0.2)
        except Exception:
            pass

    @staticmethod
    def _extract_from_dom(driver: webdriver.Chrome) -> dict:
        def read(selectors):
            for sel in selectors:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel):
                        t = el.text.strip().replace("\u20b9", "").replace(",", "").strip()
                        if t.isdigit():
                            return int(t)
                except Exception:
                    pass
            return None

        out: dict = {}
        fsp = read(FSP_SELECTORS)

        # PRIORITY: Find NEP via "Lowest price for you" label — this is what the user actually sees.
        # Run this BEFORE CSS selectors because class-based selectors can match EMI price elements.
        nep = None
        try:
            price_elements = driver.find_elements(By.XPATH,
                "//*[contains(text(), 'Lowest price for you')]/ancestor::div[2]/div[1]")
            for el in price_elements:
                text = el.text.strip()
                import re as _re2
                match = _re2.match(r'^₹\s*([\d,]+)$', text)
                if match:
                    price_str = match.group(1).replace(",", "")
                    if price_str.isdigit() and 5000 <= int(price_str) <= 500000:
                        nep = int(price_str)
                        break
        except Exception:
            pass

        # Fallback to CSS selectors only if XPath didn't find anything
        if not nep:
            nep = read(NEP_SELECTORS)
        
        if fsp:
            out["fsp"] = fsp
            out["source"] = "dom"
        if nep:
            out["nep"] = nep

        # Generic fallback: Flipkart's class names are obfuscated and change
        # often, so if the known selectors miss, scan every element for a
        # "₹<number>" text and take the most prominent one (largest font) as
        # the FSP. This survives class-name churn.
        if not out.get("fsp"):
            try:
                found = driver.execute_script(
                    """
                    const out = [];
                    const els = document.querySelectorAll('div,span');
                    for (const el of els) {
                        if (el.children.length) continue;          // leaf nodes only
                        const txt = (el.textContent || '').trim();
                        const m = txt.match(/^\\u20b9\\s*([\\d,]+)$/);
                        if (!m) continue;
                        const val = parseInt(m[1].replace(/,/g, ''), 10);
                        if (!val || val < 100) continue;           // skip tiny / EMI bits
                        const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
                        out.push([val, fs]);
                    }
                    return out;
                    """
                )
                if found:
                    # Largest font size => the headline selling price.
                    found.sort(key=lambda p: p[1], reverse=True)
                    out["fsp"] = int(found[0][0])
                    out["source"] = "dom"
            except Exception:
                pass

        return out

    @staticmethod
    def _extract_bank_offers(driver: webdriver.Chrome, html: str) -> list:
        """Extract bank offers, cashback, and discounts from the product page."""
        # Use external module if enabled
        if EXTRACT_BANK_OFFERS and extract_bank_offers:
            return extract_bank_offers(driver, html, verbose=BANK_OFFERS_VERBOSE)
        return []

    @staticmethod
    def _product_name(driver: webdriver.Chrome, html: str) -> str:
        try:
            el = driver.find_element(
                By.CSS_SELECTOR, "span.VU-ZEz, h1.yhB1nd, span.B_NuCI"
            )
            if el.text.strip():
                return el.text.strip()
        except Exception:
            pass
        m = re.search(r'"name"\s*:\s*"([^"]{10,})"', html)
        return m.group(1) if m else ""


# ── price extraction ───────────────────────────────────────────────────────
def _balanced_json_object(text: str, brace_start: int) -> str:
    depth = 0
    in_str = False
    esc = False
    for i in range(brace_start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start:i + 1]
    return ""


def extract_ppd_prices(html: str) -> dict:
    best = None
    best_score = 0
    for m in re.finditer(r'"ppd"\s*:\s*\{', html):
        brace_start = html.index("{", m.start())
        blob = _balanced_json_object(html, brace_start)
        if not blob:
            continue
        try:
            ppd = json.loads(blob)
        except Exception:
            continue
        score = sum(1 for k in ("finalPrice", "fsp", "nepPrice", "mrp",
                                "xtraSaverPrice") if ppd.get(k))
        if score > best_score:
            best_score = score
            best = ppd
    if best:
        fsp = best.get("fsp") or best.get("finalPrice")
        # nepPrice is the bank/exchange offer price. Fallback chain:
        # nepPrice → xtraSaverPrice → lowestSellingPrice → None
        nep = (best.get("nepPrice") or
               best.get("xtraSaverPrice") or
               best.get("lowestSellingPrice") or
               best.get("effectivePrice")) or None
        return {
            "fsp":    fsp,
            "nep":    nep,
            "mrp":    best.get("mrp"),
            "source": "ppd_json",
        }
    return {}


# ── CLI ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Flipkart browser price scraper")
    parser.add_argument("pids", nargs="*", help="Product PIDs to scrape")
    parser.add_argument("--delay", type=float, default=8.0,
                        help="Seconds to wait between products (default: 8)")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless")
    args = parser.parse_args()

    if not args.pids:
        print("Usage: python flipkart_scraper.py PID1 PID2 ... [--delay N] [--headless]")
        return

    results = []
    with FlipkartScraper(headless=args.headless) as scraper:
        for i, pid in enumerate(args.pids):
            print(f"\n[{datetime.now():%H:%M:%S}] {pid}", flush=True)
            data = scraper.scrape(pid)
            results.append(data)
            if data.get("error"):
                print(f"  ERROR: {data['error']}", flush=True)
            else:
                fsp = f"\u20b9{data['fsp']:,}" if data["fsp"] else "\u2014"
                nep = f"\u20b9{data['nep']:,}" if data["nep"] else "\u2014"
                mrp = f"\u20b9{data['mrp']:,}" if data["mrp"] else "\u2014"
                print(f"  {data['name']}", flush=True)
                print(f"  FSP={fsp}  NEP={nep}  MRP={mrp}  ({data['source']})", flush=True)
            if i < len(args.pids) - 1:
                time.sleep(args.delay)

    with open("scrape_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=True)
    print(f"\nSaved {len(results)} result(s) to scrape_results.json", flush=False) 0


if __name__ == "__main__":
    main()
