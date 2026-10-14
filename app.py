import json
import re
import time
import random
import queue
import threading
import certifi
import requests as req
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import atexit
from collections import deque
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
import config

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8953384321:AAGwWwD-7fkcR7hIMYrIxm-m8wZfXpKbKAs"
TELEGRAM_CHAT_ID = "1124426595"  # Vasu
MONGO_URI        = "mongodb+srv://jagsir:JWff45t518S1d8F0@cluster0.rt55a2o.mongodb.net/?appName=Cluster0"


# ── MongoDB ───────────────────────────────────────────────────────────────────
_mongo    = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
_db       = _mongo["flipkart"]
_col      = _db["price_history"]   # {pid, name, url, ts, fsp, nep}
_col_pids = _db["products"]        # {pid, name, url}
_col_subs = _db["tg_subscribers"]  # {chat_id}

# ── Shared state ──────────────────────────────────────────────────────────────
_state    = {}   # pid → latest snapshot
_last     = {}   # pid → {price, lowest_price} for change detection
_baseline = {}   # pid → {price, lowest_price} seeded from last MongoDB record
_records  = {}   # pid → {low_fsp, low_nep, high_fsp, high_nep} all-time records
_last_success = {}  # pid → ISO string of last successful 200 (persisted via MongoDB)
_last_scrape_time = {}  # pid → timestamp of last scrape (for fair rotation)
_alerts   = []
_clients  = []
_lock     = threading.Lock()

# ── Live Logs ─────────────────────────────────────────────────────────────────
_logs = deque(maxlen=200)  # Keep last 200 log lines
_log_clients = []  # SSE clients for log streaming
_app_start_time = datetime.now()  # Track when app started
_auto_restart_enabled = True  # Toggle for auto-restart - enabled by default

class LogCapture(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_line = f"[{timestamp}] {msg}"
            _logs.append(log_line)
            # Notify all log stream clients (asyncio queues)
            import asyncio
            for q in _log_clients:
                try:
                    # asyncio.Queue.put_nowait() works from sync code
                    q.put_nowait(log_line)
                except (asyncio.QueueFull, Exception):
                    pass
        except:
            pass

# Set up logging to capture print statements and logs
# File handler for activity logs (10MB max, keep 5 backups)
file_handler = RotatingFileHandler(
    'activity.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        LogCapture(),
        file_handler
    ]
)
logger = logging.getLogger(__name__)
logger.info("="*60)
logger.info("Application started")
logger.info("="*60)

# Create separate activity logger for important events only (no console spam)
activity_logger = logging.getLogger('activity')
activity_logger.setLevel(logging.INFO)
activity_logger.propagate = False  # Don't propagate to root logger
activity_handler = RotatingFileHandler('activity.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
activity_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
activity_logger.addHandler(activity_handler)

# Create console-only logger for product scrapes (not in activity.log)
console_logger = logging.getLogger('console')
console_logger.setLevel(logging.INFO)
console_logger.propagate = False  # Don't propagate to root logger
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
console_logger.addHandler(console_handler)
console_logger.addHandler(LogCapture())  # Also show in Live Logs dashboard

# Suppress noisy urllib3 retry warnings from Selenium
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
logging.getLogger('selenium.webdriver.remote.remote_connection').setLevel(logging.ERROR)

# ── Scraper abstraction ───────────────────────────────────────────────────────
# One browser instance reused across all scrapes (only when method=browser)
_browser_scraper = None
_worker_scrapers = {}


def _make_browser_scraper():
    from flipkart_scraper import FlipkartScraper
    return FlipkartScraper(
        headless=config.BROWSER_HEADLESS,
        render_wait=config.BROWSER_RENDER_WAIT,
    )


def _close_scraper(scraper):
    if scraper is None:
        return
    try:
        scraper.close()
    except Exception:
        pass


def _get_worker_scraper(worker_id: int):
    with _lock:
        scraper = _worker_scrapers.get(worker_id)
        if scraper is None:
            scraper = _make_browser_scraper()
            _worker_scrapers[worker_id] = scraper
        return scraper


def _get_display_scraper():
    global _browser_scraper
    if _browser_scraper is not None:
        return _browser_scraper
    with _lock:
        for scraper in _worker_scrapers.values():
            if scraper is not None:
                return scraper
    return None


def _reset_all_scrapers():
    global _browser_scraper
    with _lock:
        worker_scrapers = list(_worker_scrapers.values())
        _worker_scrapers.clear()
    for scraper in worker_scrapers:
        _close_scraper(scraper)
    _close_scraper(_browser_scraper)
    _browser_scraper = None

# Cleanup function to close browser on exit
def _cleanup_browser():
    _reset_all_scrapers()

# Register cleanup handler
atexit.register(_cleanup_browser)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}
BASE_URL = "https://www.flipkart.com/product/p/itm?pid={pid}&lid={lid}"


def _fetch_prices(pid: str, lid: str = "", scraper=None) -> dict:
    """Unified fetch interface. Returns:
        {http_status, size_kb, fsp, nep, status_label, error_msg, raw_html (debug)}
    """
    if config.SCRAPE_METHOD == "browser":
        global _browser_scraper
        target_scraper = scraper
        if target_scraper is None:
            if _browser_scraper is None:
                _browser_scraper = _make_browser_scraper()
            target_scraper = _browser_scraper
        
        try:
            data = target_scraper.scrape(pid, lid)
        except Exception as e:
            error_str = str(e).lower()
            # Chrome crashed or connection lost - force browser recreation
            if any(x in error_str for x in ['connection', 'winerror 10061', 'refused', 'reset']):
                logger.warning(f"🔄 Chrome crashed - forcing browser reload (Error: {e})")
                _close_scraper(target_scraper)
                if target_scraper is _browser_scraper:
                    _browser_scraper = None
                return {"http_status": None, "size_kb": 0,
                        "fsp": None, "nep": None,
                        "status_label": "RETRY", "error_msg": "Chrome crashed - will retry"}
            # Other error - propagate
            raise
        
        if data.get("error"):
            return {"http_status": None, "size_kb": 0,
                    "fsp": None, "nep": None,
                    "status_label": f"ERROR: {data['error']}", "error_msg": data["error"]}
        return {"http_status": 200, "size_kb": 0,
                "fsp": data.get("fsp"), "nep": data.get("nep"),
                "bank_offers": data.get("bank_offers", []),
                "timing": data.get("timing"),
                "status_label": "OK" if (data.get("fsp") or data.get("nep")) else "NO_PRICE",
                "error_msg": None,
                "raw_html": data.get("raw_html")}

    # ── default: lightweight HTTP/requests ────────────────────────────────
    url = BASE_URL.format(pid=pid, lid=lid) if lid else f"https://www.flipkart.com/product/p/itm?pid={pid}"
    resp = req.get(url, headers=HEADERS, timeout=10)
    size_kb = round(len(resp.text) / 1024, 1)
    if resp.status_code == 200 and len(resp.text) > 5000:
        ppd_m = re.search(r'"ppd":\{([^}]+)\}', resp.text)
        ppd = {}
        if ppd_m:
            try:
                ppd = json.loads('{' + ppd_m.group(1) + '}')
            except Exception:
                pass
        fsp = ppd.get("fsp")
        nep = ppd.get("nepPrice")
        return {"http_status": resp.status_code, "size_kb": size_kb,
                "fsp": fsp, "nep": nep,
                "status_label": "OK" if (fsp or nep) else "NO_PRICE",
                "error_msg": None}
    label = "BLOCKED" if resp.status_code == 200 else f"HTTP {resp.status_code}"
    return {"http_status": resp.status_code, "size_kb": size_kb,
            "fsp": None, "nep": None, "status_label": label, "error_msg": label}


# ── MongoDB helpers ───────────────────────────────────────────────────────────
def load_products():
    products = list(_col_pids.find({}, {"_id": 0, "pid": 1, "name": 1, "url": 1, "target_lowest": 1, "lid": 1, "last_alert_nep": 1}))
    if config.TEST_MODE and config.TEST_PIDS:
        products = [p for p in products if p["pid"] in config.TEST_PIDS]
        console_logger.info(f"[TEST MODE] Filtering to {len(products)} product(s): {config.TEST_PIDS}")
    return products


def load_baseline():
    for item in load_products():
        pid = item["pid"]
        name = item.get("name", pid)
        url = item.get("url", "")
        
        # Load latest non-null fsp and nep independently — the last row may have
        # nep=null if the offer briefly expired, so we query each field separately
        # to guarantee we seed in-memory state with the best known values.
        fsp_doc = _col.find_one({"pid": pid, "fsp": {"$gt": 0}}, sort=[("ts", DESCENDING)])
        nep_doc = _col.find_one({"pid": pid, "nep": {"$gt": 0}}, sort=[("ts", DESCENDING)])
        any_doc = _col.find_one({"pid": pid}, sort=[("ts", DESCENDING)])
        if fsp_doc or nep_doc:
            # Load into both _baseline AND _last so alerts work immediately after restart
            price_data = {
                "price":        fsp_doc.get("fsp") if fsp_doc else None,
                "lowest_price": nep_doc.get("nep") if nep_doc else None,
            }
            _baseline[pid] = price_data
            _last[pid] = price_data.copy()  # Also populate _last for immediate alerts
        doc = any_doc
        if doc:
            raw_ts = doc.get("ts_last") or doc.get("ts")
            if raw_ts:
                if hasattr(raw_ts, "tzinfo") and raw_ts.tzinfo is None:
                    _last_success[pid] = raw_ts.isoformat() + "+00:00"
                else:
                    _last_success[pid] = raw_ts.isoformat()
                # Initialize last scrape time from MongoDB for fair rotation
                _last_scrape_time[pid] = raw_ts.timestamp() if hasattr(raw_ts, "timestamp") else 0
        # Load all-time low/high fsp and nep from history
        low_fsp_doc = _col.find_one({"pid": pid, "fsp": {"$ne": None}}, sort=[("fsp", ASCENDING)])
        low_nep_doc = _col.find_one({"pid": pid, "nep": {"$ne": None}}, sort=[("nep", ASCENDING)])
        high_fsp_doc = _col.find_one({"pid": pid, "fsp": {"$ne": None}}, sort=[("fsp", DESCENDING)])
        high_nep_doc = _col.find_one({"pid": pid, "nep": {"$ne": None}}, sort=[("nep", DESCENDING)])
        _records[pid] = {
            "low_fsp": low_fsp_doc.get("fsp") if low_fsp_doc else None,
            "low_nep": low_nep_doc.get("nep") if low_nep_doc else None,
            "high_fsp": high_fsp_doc.get("fsp") if high_fsp_doc else None,
            "high_nep": high_nep_doc.get("nep") if high_nep_doc else None,
        }
        
        # Populate _state with baseline snapshot so products appear immediately on dashboard
        baseline = _baseline.get(pid, {})
        rec = _records.get(pid, {})
        _state[pid] = {
            "pid": pid,
            "name": name,
            "url": url,
            "lid": item.get("lid", ""),  # Include LID
            "target_lowest": item.get("target_lowest"),  # Include target price
            "price": baseline.get("price"),
            "lowest_price": baseline.get("lowest_price"),
            "price_prev": None,
            "lowest_prev": None,
            "price_dir": None,
            "lowest_dir": None,
            "all_time_low_fsp": rec.get("low_fsp"),
            "all_time_low_nep": rec.get("low_nep"),
            "all_time_high_fsp": rec.get("high_fsp"),
            "all_time_high_nep": rec.get("high_nep"),
            "http_status": 200,
            "size_kb": 0,
            "status": "LOADED",
            "ts": datetime.now().strftime("%H:%M:%S"),
            "last_success_ts": _last_success.get(pid),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────
def _extract_offer_amount(offer_str: str) -> int:
    """Extract the ₹ amount from an offer string."""
    m = re.search(r'₹([\d,]+)\s+off', offer_str)
    return int(m.group(1).replace(',', '')) if m else 0


def _process_bank_offers(offers: list, nep):
    """
    1. Remove fake offer lines — lines that are only '₹X,XXX off' with no bank name.
    2. If Best Value offer contains 'cashback', recalculate NEP:
         new_nep = old_nep + x - y
       where x = Best Value cashback amount, y = first non-cashback offer amount after Best Value (0 if none).
    Returns: (cleaned_offers, adjusted_nep, offer_note or None)
    """
    # Step 1: remove fake lines — pattern: just '₹digits off' with no ' - Bank' info
    def _is_fake(o):
        s = re.sub(r'^\[Best Value\]\s*-?\s*', '', o.strip(), flags=re.IGNORECASE).strip()
        return bool(re.match(r'^₹[\d,]+\s+off$', s, re.IGNORECASE))

    cleaned = [o for o in offers if not _is_fake(o)]

    if not cleaned or nep is None:
        return cleaned, nep, None

    # Step 2: find Best Value offer
    best_idx = next((i for i, o in enumerate(cleaned) if '[best value]' in o.lower()), None)
    if best_idx is None or 'cashback' not in cleaned[best_idx].lower():
        return cleaned, nep, None

    x = _extract_offer_amount(cleaned[best_idx])
    if x == 0:
        return cleaned, nep, None

    # Find first offer after Best Value that does NOT contain 'cashback'
    y = 0
    applied = None
    for o in cleaned[best_idx + 1:]:
        if 'cashback' not in o.lower():
            y = _extract_offer_amount(o)
            applied = o
            break

    adjusted_nep = nep + x - y
    best_display = re.sub(r'^\[Best Value\]\s*-?\s*', '', cleaned[best_idx], flags=re.IGNORECASE).strip()
    applied_display = re.sub(r'^\[Best Value\]\s*-?\s*', '', applied, flags=re.IGNORECASE).strip() if applied else 'none (y=0)'
    offer_note = (
        f"💡 <b>Offer swap</b> (cashback excluded):\n"
        f"   ✗ {best_display}\n"
        f"   ✓ {applied_display}\n"
        f"   NEP ₹{nep:,} → ₹{adjusted_nep:,}"
    )
    return cleaned, adjusted_nep, offer_note


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        return
    # Always include Vasu + any registered subscribers
    chat_ids = {TELEGRAM_CHAT_ID}
    for doc in _col_subs.find({}, {"chat_id": 1}):
        chat_ids.add(str(doc["chat_id"]))
    
    # Log alert to activity file (clean version without HTML tags)
    clean_msg = re.sub(r'<[^>]+>', '', msg).replace('\n', ' | ')
    activity_logger.info(f"[TELEGRAM ALERT] Sending to {len(chat_ids)} recipients: {clean_msg[:200]}")
    
    for cid in chat_ids:
        try:
            r = req.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=5,
            )
            logger.info(f"[Telegram] → {cid} status={r.status_code} ok={r.json().get('ok')}")
        except Exception as e:
            logger.info(f"[Telegram] ERROR to {cid}: {e}")


def send_telegram_screenshot(caption: str = "", scraper=None):
    """Take a screenshot of the current browser page and send to all Telegram recipients."""
    target_scraper = scraper or _get_display_scraper()
    if not TELEGRAM_TOKEN or target_scraper is None:
        return
    try:
        drv = target_scraper._driver
        if drv is None:
            return
        png = drv.get_screenshot_as_png()
        chat_ids = {TELEGRAM_CHAT_ID}
        for doc in _col_subs.find({}, {"chat_id": 1}):
            chat_ids.add(str(doc["chat_id"]))
        for cid in chat_ids:
            try:
                req.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={"chat_id": cid, "caption": caption[:1000]},
                    files={"photo": ("screenshot.png", png, "image/png")},
                    timeout=15,
                )
            except Exception as e:
                logger.info(f"[Telegram Photo] ERROR to {cid}: {e}")
    except Exception as e:
        logger.info(f"[Telegram Photo] Screenshot failed: {e}")


def broadcast(payload: dict):
    import asyncio
    data = json.dumps(payload)
    with _lock:
        dead = []
        for q in _clients:
            try:
                # asyncio.Queue.put_nowait() works from sync code
                q.put_nowait(data)
            except (asyncio.QueueFull, Exception):
                dead.append(q)
        for q in dead:
            _clients.remove(q)


# ── Per-product timing (shows where each cycle's time goes) ───────────────────
_cycle_timings = {}        # pid -> wall seconds for the product (excluding gap sleep)
_cycle_timing_detail = {}  # pid -> {"price_s":..., "offers_s":...} from the scraper


def _timed_scrape_product(item: dict, gap: float, scraper=None):
    t0 = time.time()
    try:
        _scrape_product(item, gap, scraper)
    finally:
        wall = max(0.0, time.time() - t0 - gap)
        with _lock:
            _cycle_timings[item["pid"]] = wall


def _log_timing_summary(cycle_no: int, workers: int):
    """One-line summary of where the cycle's time went. Deliberately does NOT
    contain the text 'Cycle #' (hostinger_cycle_monitor.py greps for that)."""
    with _lock:
        timings = dict(_cycle_timings)
        detail = dict(_cycle_timing_detail)
        _cycle_timings.clear()
        _cycle_timing_detail.clear()
    if not timings:
        return
    walls = sorted(timings.values())
    avg = sum(walls) / len(walls)
    slow = sorted(timings.items(), key=lambda kv: kv[1], reverse=True)[:3]
    slow_txt = ", ".join(f"{pid} {sec:.0f}s" for pid, sec in slow)
    price_vals = [d["price_s"] for d in detail.values() if d.get("price_s") is not None]
    offer_vals = [d["offers_s"] for d in detail.values() if d.get("offers_s") is not None]
    split = ""
    if price_vals and offer_vals:
        split = (f" | avg page+price {sum(price_vals)/len(price_vals):.1f}s,"
                 f" avg bank-offers {sum(offer_vals)/len(offer_vals):.1f}s")
    activity_logger.info(
        f"⏱ Timing summary (cycle {cycle_no}, {workers} worker(s)): {len(walls)} products, "
        f"avg {avg:.1f}s, median {walls[len(walls)//2]:.1f}s, max {walls[-1]:.1f}s{split} | slowest: {slow_txt}"
    )


# ── Background scraper ────────────────────────────────────────────────────────
def scrape_loop():
    cycle_count = 0
    while True:
        cycle_start = time.time()
        products = load_products()
        if not products:
            time.sleep(5)
            continue

        # Sort products by oldest scrape time first (fair rotation)
        products_sorted = sorted(
            products,
            key=lambda p: _last_scrape_time.get(p["pid"], 0)
        )

        # Dynamic gap: faster scraping
        # Adjusted for speed while avoiding rate limits
        gap = max(0.3, min(5, 60 / len(products)))

        worker_count = 1
        if config.SCRAPE_METHOD == "browser":
            worker_count = max(1, min(getattr(config, "SCRAPER_WORKERS", 1), len(products_sorted)))

        if worker_count == 1:
            for item in products_sorted:
                _timed_scrape_product(item, gap)
        else:
            batches = [products_sorted[i::worker_count] for i in range(worker_count)]
            workers = []
            for worker_id, batch in enumerate(batches):
                if not batch:
                    continue
                thread = threading.Thread(
                    target=_scrape_batch,
                    args=(worker_id, batch, gap),
                    daemon=True,
                )
                thread.start()
                workers.append(thread)
            for thread in workers:
                thread.join()
        
        # Log cycle completion
        cycle_count += 1
        cycle_time = time.time() - cycle_start
        cycle_mins = cycle_time / 60
        activity_logger.info(f"🔄 Cycle #{cycle_count} completed: {len(products)} products in {cycle_mins:.1f} min ({cycle_time:.0f}s)")
        _log_timing_summary(cycle_count, worker_count)


def _scrape_batch(worker_id: int, batch: list, gap: float):
    scraper = _get_worker_scraper(worker_id)
    for item in batch:
        _timed_scrape_product(item, gap, scraper)


def _scrape_product(item: dict, gap: float, scraper=None):
    pid  = item["pid"]
    name = item.get("name", pid)
    url  = item.get("url", "")
    lid  = item.get("lid", "")
    target_lowest = item.get("target_lowest")
    last_alert_nep = item.get("last_alert_nep")
    ts   = datetime.now().strftime("%H:%M:%S")

    with _lock:
        rec  = dict(_records.get(pid, {}))
        last = dict(_last.get(pid, _baseline.get(pid, {})))
        last_success_ts = _last_success.get(pid)

    snapshot = {
        "pid": pid, "name": name, "url": url,
        "lid": lid,
        "target_lowest": target_lowest,
        "price":        last.get("price"),
        "lowest_price": last.get("lowest_price"),
        "price_prev": None, "lowest_prev": None,
        "price_dir": None,  "lowest_dir": None,
        "all_time_low_fsp": rec.get("low_fsp"),
        "all_time_low_nep": rec.get("low_nep"),
        "all_time_high_fsp": rec.get("high_fsp"),
        "all_time_high_nep": rec.get("high_nep"),
        "http_status": None, "size_kb": 0,
        "status": "PENDING", "ts": ts,
        "last_success_ts": last_success_ts,
    }

    active_scraper = scraper or _get_display_scraper()

    try:
        fetched = _fetch_prices(pid, lid, scraper=scraper)
        snapshot["http_status"] = fetched["http_status"]
        snapshot["size_kb"]     = fetched["size_kb"]
        if fetched.get("timing"):
            with _lock:
                _cycle_timing_detail[pid] = fetched["timing"]

        if active_scraper is not None:
            browser_login = getattr(active_scraper, "login_confirmed", False)
            browser_account = getattr(active_scraper, "account_name", None)
            if not browser_login and not browser_account:
                err_msg = f"[LOGIN NOT CONFIRMED] pid={pid} account_name={browser_account!r} - continuing without forced exit"
                activity_logger.warning(err_msg)
                console_logger.warning(f"⚠️ {err_msg}")

        if fetched["status_label"] in ("OK", "NO_PRICE"):
            price  = fetched["fsp"]
            lowest = fetched["nep"]
            bank_offers = fetched.get("bank_offers", [])
            _raw_nep = fetched["nep"]
            bank_offers, lowest, _offer_note = _process_bank_offers(bank_offers, lowest)
            if not bank_offers:
                console_logger.info("  ↻ No live offers found, retrying scrape once...")
                time.sleep(1.0)
                retry_fetched = _fetch_prices(pid, lid, scraper=scraper)
                if retry_fetched["status_label"] in ("OK", "NO_PRICE"):
                    retry_offers = retry_fetched.get("bank_offers", [])
                    retry_raw_nep = retry_fetched["nep"]
                    retry_offers, retry_lowest, retry_offer_note = _process_bank_offers(retry_offers, retry_raw_nep)
                    if retry_offers:
                        fetched = retry_fetched
                        snapshot["http_status"] = retry_fetched["http_status"]
                        snapshot["size_kb"] = retry_fetched["size_kb"]
                        price = retry_fetched["fsp"]
                        lowest = retry_lowest
                        bank_offers = retry_offers
                        _raw_nep = retry_raw_nep
                        _offer_note = retry_offer_note
                        console_logger.info(f"  ✓ Offers recovered on retry ({len(bank_offers)} found)")
            if not bank_offers:
                console_logger.info(f"⏭ {pid} skipped: no live bank offers after retry")
                console_logger.info("")
                time.sleep(gap)
                return
            if _offer_note and _raw_nep and lowest != _raw_nep:
                console_logger.info(f"  → NEP adjusted: ₹{_raw_nep:,} → ₹{lowest:,} (Δ{lowest - _raw_nep:+,})")
            if not price:
                price = last.get("price")
            if not lowest:
                lowest = last.get("lowest_price")
            snapshot["price"]        = price
            snapshot["lowest_price"] = lowest
            snapshot["bank_offers"]  = bank_offers
            snapshot["status"]       = "OK" if (price or lowest) else "NO_PRICE"

            if price or lowest:
                now_utc = datetime.now(timezone.utc)
                latest  = _col.find_one({"pid": pid}, sort=[("ts", DESCENDING)])
                if latest and latest.get("fsp") == price and latest.get("nep") == lowest:
                    _col.update_one({"_id": latest["_id"]}, {"$set": {"ts_last": now_utc}})
                else:
                    _col.insert_one({
                        "pid": pid, "name": name, "url": url,
                        "ts": now_utc, "ts_last": now_utc,
                        "fsp": price, "nep": lowest,
                    })
                with _lock:
                    _last_success[pid] = now_utc.isoformat()
                snapshot["last_success_ts"] = now_utc.isoformat()

            price_changed = False
            price_dir = None
            lowest_changed = False
            lowest_dir = None

            if price is not None:
                prev = last.get("price")
                if prev is None or price != prev:
                    price_changed = True
                    price_dir = "DOWN" if prev is not None and price < prev else "UP" if prev is not None else "NEW"
                    if prev is not None:
                        snapshot["price_prev"] = prev
                    snapshot["price_dir"] = price_dir

            if lowest is not None:
                prev = last.get("lowest_price")
                if prev is None or lowest != prev:
                    lowest_changed = True
                    lowest_dir = "DOWN" if prev is not None and lowest < prev else "UP" if prev is not None else "NEW"
                    if prev is not None:
                        snapshot["lowest_prev"] = prev
                    snapshot["lowest_dir"] = lowest_dir

            should_alert = False

            if target_lowest:
                if lowest is not None and lowest <= target_lowest and lowest != last_alert_nep:
                    should_alert = True
                elif lowest is not None and lowest > target_lowest and last_alert_nep is not None:
                    last_alert_nep = None
                    _col_pids.update_one({"pid": pid}, {"$set": {"last_alert_nep": None}})
            else:
                if price_changed or lowest_changed:
                    should_alert = True

            if should_alert and lowest is not None:
                _prev_nep = last.get("lowest_price")
                if _prev_nep is not None and abs(lowest - _prev_nep) <= 4:
                    should_alert = False
                    activity_logger.info(
                        f"⏭ Alert suppressed (NEP Δ=₹{abs(lowest - _prev_nep)} ≤ ₹4): {name} "
                        f"prev=₹{_prev_nep:,} now=₹{lowest:,}"
                    )

            if should_alert:
                if price_changed or lowest_changed:
                    overall = price_dir if price_changed else lowest_dir
                    if price_changed and lowest_changed and price_dir != lowest_dir:
                        overall = "CHANGED"
                else:
                    overall = "TARGET"

                nep_flag = " ⚡ (Adjusted)" if _offer_note else ""
                price_block = ""
                if price is not None:
                    price_block += f"💵 FSP    <b>₹{price:,}</b>\n"
                if lowest is not None:
                    price_block += f"📉 NEP    <b>₹{lowest:,}</b>{nep_flag}\n"
                if target_lowest:
                    price_block += f"🎯 Target  <b>₹{target_lowest:,}</b>"

                adj_block = ""
                if _offer_note:
                    adj_block = f"\n\n{_offer_note}"

                offers_block = ""
                if bank_offers:
                    lines = []
                    for i, offer in enumerate(bank_offers, 1):
                        lines.append(f"  {i}. {offer}")
                    offers_block = f"\n\n💳 <b>{len(bank_offers)} offers:</b>\n" + "\n".join(lines)

                product_link = f'<a href="{url}">{name}</a>'

                if target_lowest and lowest is not None and lowest <= target_lowest:
                    msg = (
                        f"🎯 <b>TARGET HIT</b>\n\n"
                        f"🛍 <b>{product_link}</b>\n\n"
                        f"{price_block}"
                        f"{adj_block}"
                        f"{offers_block}"
                    )
                elif overall == "DOWN":
                    msg = (
                        f"🔻 <b>PRICE DROPPED</b>\n\n"
                        f"🛍 <b>{product_link}</b>\n\n"
                        f"{price_block}"
                        f"{adj_block}"
                        f"{offers_block}"
                    )
                elif overall == "UP":
                    msg = (
                        f"🔺 <b>PRICE INCREASED</b>\n\n"
                        f"🛍 <b>{product_link}</b>\n\n"
                        f"{price_block}"
                        f"{adj_block}"
                        f"{offers_block}"
                    )
                else:
                    msg = (
                        f"🔶 <b>PRICE CHANGED</b>\n\n"
                        f"🛍 <b>{product_link}</b>\n\n"
                        f"{price_block}"
                        f"{adj_block}"
                        f"{offers_block}"
                    )

                send_telegram(msg)
                if config.ALERT_SCREENSHOT:
                    threading.Thread(
                        target=send_telegram_screenshot,
                        args=(f"{name} ({pid})", active_scraper),
                        daemon=True,
                    ).start()
                if target_lowest and lowest is not None:
                    _col_pids.update_one({"pid": pid}, {"$set": {"last_alert_nep": lowest}})
                with _lock:
                    _alerts.append({"ts": ts, "timestamp": time.time(), "pid": pid, "name": name, "msg": msg})
                    if len(_alerts) > 100:
                        _alerts.pop(0)
                activity_logger.info(f"📢 Alert sent: {name} - NEP ₹{lowest:,} (Target: ₹{target_lowest:,})" if target_lowest else f"📢 Alert sent: {name}")

            with _lock:
                _last[pid] = {"price": price, "lowest_price": lowest}
                if pid not in _baseline:
                    _baseline[pid] = {"price": price, "lowest_price": lowest}
                rec = _records.setdefault(pid, {"low_fsp": None, "low_nep": None, "high_fsp": None, "high_nep": None})
                if price is not None:
                    if rec["low_fsp"] is None or price < rec["low_fsp"]:
                        rec["low_fsp"] = price
                    if rec["high_fsp"] is None or price > rec["high_fsp"]:
                        rec["high_fsp"] = price
                if lowest is not None:
                    if rec["low_nep"] is None or lowest < rec["low_nep"]:
                        rec["low_nep"] = lowest
                    if rec["high_nep"] is None or lowest > rec["high_nep"]:
                        rec["high_nep"] = lowest
                snapshot["all_time_low_fsp"] = rec["low_fsp"]
                snapshot["all_time_low_nep"] = rec["low_nep"]
                snapshot["all_time_high_fsp"] = rec["high_fsp"]
                snapshot["all_time_high_nep"] = rec["high_nep"]

            fsp_str = f"FSP ₹{price:,}" if price else "FSP —"
            nep_str = f"NEP ₹{lowest:,}" if lowest else "NEP —"

            login_status = ""
            if active_scraper and hasattr(active_scraper, 'account_name'):
                if active_scraper.account_name:
                    login_status = f" [{active_scraper.account_name}]"
                else:
                    login_status = " [🚨 not logged in]"

            offers_info = f" ({len(bank_offers)} offers)" if bank_offers else ""
            console_logger.info(f"{fetched['http_status'] or 200} {pid}  {fsp_str}  {nep_str}{offers_info}{login_status}")

            if bank_offers:
                for i, offer in enumerate(bank_offers, 1):
                    console_logger.info(f"  {i}. {offer}")
            console_logger.info("")

        else:
            snapshot["status"] = fetched["status_label"]
            if "BLOCKED" not in fetched["status_label"]:
                console_logger.info(f"{fetched['http_status'] or '—'} {pid}  {snapshot['status']}")

    except req.exceptions.Timeout:
        snapshot["status"] = "TIMEOUT"
        console_logger.info(f" — {pid}  TIMEOUT")
    except Exception as e:
        snapshot["status"] = f"ERROR: {type(e).__name__}"
        console_logger.info(f"[{ts}]  — {pid}  ERROR: {type(e).__name__}")

    if not _col_pids.find_one({"pid": pid}, {"_id": 1}):
        return

    with _lock:
        _state[pid] = snapshot
        _last_scrape_time[pid] = time.time()
    broadcast({"type": "product", "data": snapshot})
    time.sleep(gap)


# ── Telegram bot helpers ──────────────────────────────────────────────────────
import html as _html

_tg_awaiting = {}   # chat_id → "add" while waiting for a product link

HELP_TEXT = (
    "🤖 <b>Flipkart Price Monitor</b>\n\n"
    "<b>Add a product</b>\n"
    "• Paste a Flipkart link any time, or\n"
    "• /addproduct then send the link\n\n"
    "<b>View tracked products</b>\n"
    "• /list — see all tracked products with live prices\n\n"
    "<b>Alerts</b>\n"
    "• /start subscribe · /stop pause\n"
    "• You get a 🔥 alert whenever a tracked price drops."
)


def _tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                 json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error(f"[Telegram] sendMessage failed: {resp.text}")
    except Exception as e:
        logger.error(f"[Telegram] sendMessage error: {e}")

def _tg_edit(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                 json=payload, timeout=5)
    except Exception as e:
        logger.error(f"[Telegram] editMessage error: {e}")

def _tg_answer_callback(cb_id, text=""):
    try:
        req.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                 json={"callback_query_id": cb_id, "text": text}, timeout=5)
    except Exception:
        pass


def _set_bot_commands():
    try:
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
            json={"commands": [
                {"command": "list",       "description": "View tracked products with live prices"},
                {"command": "addproduct", "description": "Add a product (send a Flipkart link)"},
                {"command": "help",       "description": "How to use this bot"},
                {"command": "start",      "description": "Subscribe to price alerts"},
                {"command": "stop",       "description": "Pause price alerts"},
            ]},
            timeout=5,
        )
    except Exception as e:
        pass


def _name_from_slug(slug: str) -> str:
    out = []
    for w in slug.replace("-", " ").split():
        wl = w.lower()
        if re.fullmatch(r"\d+gb", wl):
            out.append(wl[:-2] + "GB")
        elif re.fullmatch(r"\d+tb", wl):
            out.append(wl[:-2] + "TB")
        elif wl in ("gb", "tb", "3g", "4g", "5g"):
            out.append(wl.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _parse_flipkart_url(raw: str):
    raw = (raw or "").strip()
    pid = name = clean_url = lid = ""
    m = re.search(r"[?&]pid=([A-Za-z0-9]+)", raw)
    if m:
        pid = m.group(1).upper()
    # Extract LID if present
    lid_m = re.search(r"[?&]lid=([A-Za-z0-9]+)", raw)
    if lid_m:
        lid = lid_m.group(1)
    # Handles both www.flipkart.com/<slug>/p/<itm> and
    # dl.flipkart.com/dl/<slug>/p/<itm> (share/deep-link URLs)
    slug_m = re.search(r"flipkart\.com/(?:dl/)?([^/]+)/p/([^/?#&]+)", raw, re.I)
    if slug_m:
        slug, itm = slug_m.group(1), slug_m.group(2)
        name = _name_from_slug(slug)
        if pid:
            clean_url = f"https://www.flipkart.com/{slug}/p/{itm}?pid={pid}"
            if lid:
                clean_url += f"&lid={lid}"
    return pid, name, clean_url, lid


def _register_product(raw: str, custom_name: str = "", target_lowest: int = None, custom_lid: str = "") -> dict:
    raw = (raw or "").strip()
    pid, derived_name, clean_url, derived_lid = _parse_flipkart_url(raw)
    if not pid:
        bare = raw.upper()
        if re.fullmatch(r'[A-Z0-9]{6,25}', bare):
            pid = bare
    if not pid or not re.match(r'^[A-Z0-9]{6,25}$', pid):
        return {"status": "invalid", "error": "Couldn't find a valid PID. Paste the full Flipkart product URL."}
    
    # Use custom_lid if provided, otherwise use derived_lid from URL
    lid = (custom_lid or "").strip() or derived_lid
    
    # Validate LID is present for new products (required from now on)
    # Old products without LID will keep working (backward compatibility)
    if not lid:
        return {"status": "invalid", "error": "LID not found. Please use the full product URL with seller info (should contain &lid=...)."}
    
    existing = _col_pids.find_one({"pid": pid})
    if existing:
        return {"status": "exists", "pid": pid,
                "name": existing.get("name", pid), "url": existing.get("url", ""), "lid": existing.get("lid", "")}
    final_name = (custom_name or "").strip() or derived_name or pid
    url = clean_url or f"https://www.flipkart.com/product/p/itm?pid={pid}"
    if lid and "&lid=" not in url:
        url += f"&lid={lid}"
    
    doc = {"pid": pid, "name": final_name, "url": url, "lid": lid}
    if target_lowest and target_lowest > 0:
        doc["target_lowest"] = target_lowest
    _col_pids.insert_one(doc)
    return {"status": "added", "pid": pid, "name": final_name, "url": url, "lid": lid}


def _remove_product(pid: str) -> bool:
    pid = (pid or "").strip().upper()
    res = _col_pids.delete_one({"pid": pid})
    if res.deleted_count == 0:
        return False
    _col.delete_many({"pid": pid})
    _state.pop(pid, None)
    _last.pop(pid, None)
    _baseline.pop(pid, None)
    _records.pop(pid, None)
    broadcast({"type": "delete", "pid": pid})
    return True


def _tg_list_view():
    """Generate product list view (for callback edits - shows summary)"""
    products = load_products()
    if not products:
        return ("📭 No products tracked yet.\nSend a Flipkart link or /addproduct to start.", None)
    
    # For callback edits, show summary only (no links to avoid entity limit)
    lines = [f"🛒 <b>Tracking {len(products)} products</b>\n"]
    for i, p in enumerate(products[:20]):  # Show max 20
        pid = p["pid"]
        name = p.get("name", pid)
        snap = _state.get(pid, {})
        price = snap.get("price")
        lowest = snap.get("lowest_price")
        ps = f"₹{price:,}" if price else "—"
        ls = f"₹{lowest:,}" if lowest else "—"
        lines.append(f"<b>{i+1}.</b> {_html.escape(name)}\n   💰 {ps}  |  🏷 {ls}")
    
    if len(products) > 20:
        lines.append(f"\n... and {len(products) - 20} more")
    
    lines.append(f"\nUse /list to see full details")
    return ("\n".join(lines), None)


def _tg_send_product_list(chat_id):
    """Send full product list in chunks to avoid Telegram entity limit"""
    products = load_products()
    if not products:
        _tg_send(chat_id, "📭 No products tracked yet.\nSend a Flipkart link or /addproduct to start.")
        return
    
    # Send in chunks of 15 products to avoid ENTITIES_TOO_LONG
    chunk_size = 15
    total_chunks = (len(products) + chunk_size - 1) // chunk_size
    
    for chunk_idx in range(total_chunks):
        start = chunk_idx * chunk_size
        end = min(start + chunk_size, len(products))
        chunk = products[start:end]
        
        if total_chunks > 1:
            lines = [f"🛒 <b>Products {start+1}-{end} of {len(products)}</b> (Page {chunk_idx+1}/{total_chunks})\n"]
        else:
            lines = [f"🛒 <b>Tracking {len(products)} product{'s' if len(products) != 1 else ''}</b>\n"]
        
        for i, p in enumerate(chunk, start=start):
            pid = p["pid"]
            name = p.get("name", pid)
            url = p.get("url", f"https://www.flipkart.com/product/p/itm?pid={pid}")
            snap = _state.get(pid, {})
            price = snap.get("price")
            lowest = snap.get("lowest_price")
            ps = f"₹{price:,}" if price else "—"
            ls = f"₹{lowest:,}" if lowest else "—"
            product_link = f'<a href="{url}">{_html.escape(name)}</a>'
            lines.append(f"<b>{i+1}.</b> {product_link}\n   💰 Price: {ps}  |  🏷 Lowest: {ls}")
        
        _tg_send(chat_id, "\n".join(lines))


def _tg_add_from_text(chat_id, fname, raw):
    result = _register_product(raw)
    if result["status"] == "added":
        reply = (f"✅ Now tracking <b>{_html.escape(result['name'])}</b>\n"
                 f"You'll get a 🔥 alert when the price drops.")
    elif result["status"] == "exists":
        reply = f"ℹ️ <b>{_html.escape(result['name'])}</b> is already being tracked."
    else:
        error_msg = result.get("error", "I couldn't find a Flipkart product in that.")
        reply = f"❌ {error_msg}\nSend the full product URL (should contain pid=... and lid=...)."
    _tg_send(chat_id, reply)
    pass


def _tg_handle_callback(cb):
    data = cb.get("data", "")
    cb_id = cb.get("id")
    message = cb.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    msg_id = message.get("message_id")
    if data.startswith("rm:"):
        pid = data[3:]
        ok = _remove_product(pid)
        _tg_answer_callback(cb_id, "Removed ✅" if ok else "Already removed")
        view_text, markup = _tg_list_view()
        _tg_edit(chat_id, msg_id, view_text, markup)
    else:
        _tg_answer_callback(cb_id)


def telegram_poll_loop():
    """Long-poll Telegram for commands, product links, and inline buttons."""
    offset = None
    error_count = 0
    max_errors = 10
    
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset
            r = req.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=35,
            )
            
            # Reset error count on successful request
            error_count = 0
            
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    try:
                        _tg_handle_callback(upd["callback_query"])
                    except Exception as e:
                        logger.error(f"[Telegram] callback handler error: {e}")
                    continue
                msg = upd.get("message", {})
                raw_text = (msg.get("text") or "").strip()
                text    = raw_text.lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                fname   = msg.get("chat", {}).get("first_name", "there")
                if not chat_id or not raw_text:
                    continue
                cmd = text.split()[0].split("@")[0]
                if cmd in ("/start", "start", "hi", "hello"):
                    _col_subs.update_one(
                        {"chat_id": chat_id},
                        {"$set": {"chat_id": chat_id, "name": fname}},
                        upsert=True,
                    )
                    _tg_send(chat_id, f"👋 Hi {fname}! You're now subscribed to Flipkart price alerts.\n\nSend a Flipkart link to track a product, or tap /list to see what's tracked.")
                    pass
                elif cmd == "/stop":
                    _col_subs.delete_one({"chat_id": chat_id})
                    _tg_send(chat_id, "✅ Unsubscribed. Send /start to re-subscribe.")
                    pass
                elif cmd in ("/help", "help"):
                    _tg_awaiting.pop(chat_id, None)
                    _tg_send(chat_id, HELP_TEXT)
                elif cmd in ("/list", "/products"):
                    _tg_awaiting.pop(chat_id, None)
                    _tg_send_product_list(chat_id)
                elif cmd == "/addproduct":
                    arg = raw_text[len("/addproduct"):].strip().lstrip("@").strip()
                    if arg and "flipkart" in arg.lower() or (arg and re.search(r'[?&]pid=', arg)):
                        _tg_awaiting.pop(chat_id, None)
                        _tg_add_from_text(chat_id, fname, arg)
                    else:
                        _tg_awaiting[chat_id] = "add"
                        _tg_send(chat_id, "📎 Send me the Flipkart product link now and I'll track it.\n(Or just paste a link any time.)")
                elif _tg_awaiting.get(chat_id) == "add":
                    _tg_awaiting.pop(chat_id, None)
                    _tg_add_from_text(chat_id, fname, raw_text)
                elif ("flipkart.com" in text) or ("pid=" in text) or (
                    re.fullmatch(r'[A-Za-z0-9]{12,25}', raw_text) is not None
                    and any(ch.isdigit() for ch in raw_text)
                    and any(ch.isalpha() for ch in raw_text)
                ):
                    _tg_add_from_text(chat_id, fname, raw_text)
                    
        except req.exceptions.Timeout:
            # Timeout errors are common with long polling, just retry
            error_count += 1
            if error_count <= 3:
                logger.warning(f"[Telegram] Polling timeout (#{error_count}), retrying...")
            elif error_count > max_errors:
                logger.error(f"[Telegram] Too many consecutive errors ({error_count}), pausing for 60s")
                time.sleep(60)
                error_count = 0
            time.sleep(2)
            
        except req.exceptions.ConnectionError as e:
            # Network issues - wait longer before retry
            error_count += 1
            logger.error(f"[Telegram] Connection error: {e}")
            backoff = min(30, 5 * error_count)
            time.sleep(backoff)
            
        except Exception as e:
            # Unexpected errors - log and continue with backoff
            error_count += 1
            logger.error(f"[Telegram] Unexpected error in poll loop: {e}")
            backoff = min(30, 5 * error_count)
            if error_count > max_errors:
                logger.error(f"[Telegram] Too many errors ({error_count}), pausing for 2 minutes")
                time.sleep(120)
                error_count = 0
            else:
                time.sleep(backoff)


HEARTBEAT_INTERVAL = 60 * 10  # every 10 minutes


def heartbeat_loop():
    time.sleep(30)  # wait for first scrape cycle to complete
    while True:
        try:
            products = load_products()
            recent = _alerts[-5:] if _alerts else []  # Get last 5 alerts
            lines = ["📊 <b>Price Update</b>\n"]

            num_emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
            for i, p in enumerate(products):
                snap   = _state.get(p["pid"], {})
                price  = snap.get("price")
                lowest = snap.get("lowest_price")
                name   = p.get("name", p["pid"])
                p_str  = f"₹{price:,}" if price else "—"
                l_str  = f"₹{lowest:,}" if lowest else "—"
                lines.append(f"<b>{i+1}.</b> <b>{_html.escape(name)}</b>\n   💰 Price: {p_str}  |  🏷 Lowest: {l_str}\n")
            if recent:
                lines.append("\n🚨 <b>Recent price changes:</b>")
                for j, a in enumerate(reversed(recent), 1):
                    change_line = a["msg"].split(chr(10))[-1]
                    lines.append(f"  {j}. {_html.escape(a['name'])} — {change_line}")
            else:
                lines.append("\n🟢 No price changes since last update")

            send_telegram("\n".join(lines))
        except Exception:
            pass
        time.sleep(HEARTBEAT_INTERVAL)


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    _col.create_index([("pid", ASCENDING), ("ts", DESCENDING)])
    _col_pids.create_index("pid", unique=True)
    _col_subs.create_index("chat_id", unique=True)
    load_baseline()
    _set_bot_commands()
    threading.Thread(target=scrape_loop,        daemon=True).start()
    # threading.Thread(target=heartbeat_loop,     daemon=True).start()  # Disabled: no periodic updates
    threading.Thread(target=telegram_poll_loop, daemon=True).start()
    threading.Thread(target=auto_restart_timer, daemon=True).start()
    threading.Thread(target=health_check_loop,  daemon=True).start()
    yield
    _reset_all_scrapers()


app = FastAPI(lifespan=lifespan)

# Skip ngrok browser warning for all responses
class NgrokBypassMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["ngrok-skip-browser-warning"] = "true"
        return response

app.add_middleware(NgrokBypassMiddleware)


# ── Request models ────────────────────────────────────────────────────────────
class AddProduct(BaseModel):
    url:  str = ""
    pid:  str = ""
    name: str = ""
    target_lowest: int = None  # Optional price target for alerts
    lid: str = ""  # Listing ID for seller-specific tracking

class EditProduct(BaseModel):
    name: str = ""
    target_lowest: int = None
    lid: str = ""  # Allow editing LID


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/hello")
def hello():
    return {"ok": True, "ts": datetime.now().isoformat()}


@app.get("/test-telegram")
def test_telegram():
    send_telegram("🔔 <b>Test</b> — Flipkart Monitor is working!")
    return {"sent": True}


@app.post("/reload-browser")
def reload_browser():
    """Force close and recreate browser to reload fresh cookies"""
    _reset_all_scrapers()
    logger.info("🔄 Browser closed - will reload with fresh cookies on next scrape")
    return {"ok": True, "message": "Browser will reload on next scrape"}


@app.get("/debug-scrape/{pid}")
def debug_scrape(pid: str):
    """Debug endpoint: scrape a product and save HTML for analysis"""
    try:
        logger.info(f"🔍 Debug scraping {pid}")
        # Get LID from database if available
        product = _col_pids.find_one({"pid": pid.strip().upper()})
        lid = product.get("lid", "") if product else ""
        fetched = _fetch_prices(pid, lid)
        
        # Save HTML to file
        html_file = None
        if fetched.get("raw_html"):
            html_file = f"debug_{pid}_{int(time.time())}.html"
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(fetched.get("raw_html"))
            logger.info(f"💾 Saved HTML to {html_file} ({len(fetched.get('raw_html'))} bytes)")
        
        # Return detailed breakdown
        return {
            "pid": pid,
            "status_label": fetched.get("status_label"),
            "fsp": fetched.get("fsp"),
            "nep": fetched.get("nep"),
            "http_status": fetched.get("http_status"),
            "html_saved": html_file,
            "html_size": len(fetched.get("raw_html", "")),
            "account": _get_display_scraper().account_name if _get_display_scraper() else None
        }
    except Exception as e:
        logger.error(f"Debug scrape failed: {e}")
        return {"error": str(e)}


@app.get("/products")
def get_products():
    return list(_state.values())


@app.get("/activity-logs")
def get_activity_logs(limit: int = 100):
    """Return recent activity logs from activity.log file"""
    try:
        logs = []
        if os.path.exists("activity.log"):
            with open("activity.log", "r", encoding="utf-8") as f:
                # Read all lines and get last 'limit' lines
                all_lines = f.readlines()
                recent_lines = all_lines[-limit:] if len(all_lines) > limit else all_lines
                # Show newest at bottom (no reverse)
                logs = [line.strip() for line in recent_lines if line.strip()]
        return {"logs": logs, "count": len(logs)}
    except Exception as e:
        logger.error(f"Error reading activity logs: {e}")
        return {"logs": [], "count": 0, "error": str(e)}


@app.post("/products")
def add_product(body: AddProduct):
    result = _register_product(body.url or body.pid, body.name, body.target_lowest, body.lid)
    if result["status"] == "invalid":
        error_msg = result.get("error", "Couldn't find a valid PID and LID. Paste the full Flipkart product URL.")
        raise HTTPException(status_code=400, detail=error_msg)
    if result["status"] == "exists":
        raise HTTPException(status_code=409, detail="Product is already being tracked")
    
    # Log successful product addition to activity log
    activity_logger.info(f"[PRODUCT ADD] PID={result['pid']}, LID={result.get('lid', 'None')}, Name={result['name']}, Target={body.target_lowest or 'None'}")
    return {"ok": True, "pid": result["pid"], "name": result["name"], "url": result["url"], "lid": result.get("lid", "")}


@app.put("/products/{pid}")
def edit_product(pid: str, body: EditProduct):
    """Update product name, target_lowest price, and/or LID"""
    pid = pid.strip().upper()
    product = _col_pids.find_one({"pid": pid})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    update_data = {}
    changes = []
    if body.name and body.name.strip():
        update_data["name"] = body.name.strip()
        changes.append(f"Name: '{product.get('name')}' → '{update_data['name']}'")
    if body.target_lowest is not None:
        old_target = product.get('target_lowest')
        if body.target_lowest > 0:
            update_data["target_lowest"] = body.target_lowest
            changes.append(f"Target: {old_target or 'None'} → ₹{body.target_lowest:,}")
            if old_target != body.target_lowest:
                activity_logger.info(f"[TARGET CHANGE] PID={pid}, old: {old_target}, new: {body.target_lowest}")
        else:
            # If 0 or negative, remove the target
            update_data["target_lowest"] = None
            update_data["last_alert_nep"] = None
            changes.append(f"Target: {old_target or 'None'} → Removed")
            if old_target is not None:
                activity_logger.info(f"[TARGET REMOVED] PID={pid}")
    if body.lid and body.lid.strip():
        update_data["lid"] = body.lid.strip()
        changes.append(f"LID: '{product.get('lid', 'None')}' → '{update_data['lid']}'")
        # Update URL with new LID
        current_url = product.get("url", "")
        if current_url:
            # Remove old lid parameter if present
            import re
            new_url = re.sub(r'&lid=[^&]*', '', current_url)
            # Add new lid
            if '?' in new_url:
                new_url += f"&lid={update_data['lid']}"
            else:
                new_url += f"?lid={update_data['lid']}"
            update_data["url"] = new_url
    
    if update_data:
        _col_pids.update_one({"pid": pid}, {"$set": update_data})
        # Update in-memory state
        if pid in _state:
            if "name" in update_data:
                _state[pid]["name"] = update_data["name"]
            if "target_lowest" in update_data:
                _state[pid]["target_lowest"] = update_data["target_lowest"]
            if "lid" in update_data:
                _state[pid]["lid"] = update_data["lid"]
            if "url" in update_data:
                _state[pid]["url"] = update_data["url"]
            broadcast({"type": "product", "data": _state[pid]})
        
        # Log the edit to activity log
        activity_logger.info(f"[PRODUCT EDIT] PID={pid}, Changes: {', '.join(changes)}")
    
    return {"ok": True, "pid": pid}


@app.delete("/products/{pid}")
def delete_product(pid: str):
    pid_upper = pid.strip().upper()
    # Get product name before deletion for logging
    product = _col_pids.find_one({"pid": pid_upper})
    product_name = product.get("name", "Unknown") if product else "Unknown"
    
    if not _remove_product(pid):
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Log the deletion to activity log
    activity_logger.info(f"[PRODUCT DELETE] PID={pid_upper}, Name={product_name}")
    return {"ok": True, "pid": pid_upper}


@app.get("/alerts")
def get_alerts():
    return list(reversed(_alerts))


@app.get("/api/screenshot/{pid}", response_class=HTMLResponse)
def screenshot_product(pid: str):
    """Take a screenshot of the product page and show it alongside last scrape data."""
    import base64, threading
    from fastapi.responses import HTMLResponse
    from flipkart_scraper import FlipkartScraper

    pid = pid.strip().upper()
    product = _col_pids.find_one({"pid": pid}, {"_id": 0})
    if not product:
        return HTMLResponse(f"<h2>Product {pid} not found</h2>", status_code=404)

    url = product.get("url") or f"https://www.flipkart.com/product/p/itm?pid={pid}"
    lid = product.get("lid", "")
    if lid:
        url = f"https://www.flipkart.com/product/p/itm?pid={pid}&lid={lid}"

    # Use a separate browser so we don't interrupt the scrape cycle
    screenshot_b64 = None
    error_msg = None
    fresh_fsp = None
    fresh_nep = None
    fresh_offers = []
    try:
        scraper = FlipkartScraper(headless=True, render_wait=2)
        try:
            result = scraper.scrape(pid, lid)  # fresh scrape with cookies
            fresh_fsp = result.get("fsp")
            fresh_nep = result.get("nep")
            fresh_offers, fresh_nep, _ = _process_bank_offers(result.get("bank_offers", []), fresh_nep)
            drv = scraper._driver
            drv.execute_script("window.scrollBy(0, -Math.floor(window.innerHeight));")
            import time; time.sleep(0.3)
            png = drv.get_screenshot_as_png()
            screenshot_b64 = base64.b64encode(png).decode()
        finally:
            scraper.close()
    except Exception as e:
        error_msg = str(e)

    # Fall back to cached state if fresh scrape failed
    snap = _state.get(pid, {})
    fsp = fresh_fsp or snap.get("price")
    nep = fresh_nep or snap.get("lowest_price")
    offers = fresh_offers or snap.get("bank_offers", [])
    status = "LIVE" if fresh_fsp else snap.get("status", "—")
    last_ts = "just now" if fresh_fsp else snap.get("last_success_ts", "—")

    offers_html = "".join(f"<li>{o}</li>" for o in offers) if offers else "<li>No offers</li>"
    img_html = f'<img src="data:image/png;base64,{screenshot_b64}" style="max-width:100%;border-radius:8px;margin-top:16px">' if screenshot_b64 else f'<p style="color:red">Screenshot failed: {error_msg}</p>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Screenshot: {pid}</title>
<style>
  body {{ font-family: monospace; background: #0a0a10; color: #e4e4e7; padding: 24px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #3b82f6; font-size: 1.2rem; }}
  .info {{ background: #13131c; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .label {{ color: #52525b; font-size: 0.75rem; text-transform: uppercase; }}
  .val {{ color: #e4e4e7; font-size: 1rem; font-weight: 600; }}
  ol {{ margin: 8px 0; padding-left: 20px; color: #a1a1aa; font-size: 0.85rem; line-height: 1.7; }}
</style></head><body>
<h1>🔍 {product.get('name', pid)} <span style="color:#52525b;font-size:0.85rem">({pid})</span></h1>
<div class="info">
  <span class="label">FSP</span> <span class="val">{f'₹{fsp:,}' if fsp else '—'}</span> &nbsp;&nbsp;
  <span class="label">NEP</span> <span class="val">{f'₹{nep:,}' if nep else '—'}</span> &nbsp;&nbsp;
  <span class="label">Status</span> <span class="val">{status}</span> &nbsp;&nbsp;
  <span class="label">Last scraped</span> <span class="val">{last_ts}</span>
</div>
<div class="info">
  <span class="label">Bank Offers ({len(offers)})</span>
  <ol>{offers_html}</ol>
</div>
{img_html}
</body></html>"""
    return HTMLResponse(html)


@app.get("/api/history/{pid}")
def get_price_history(pid: str, days: int = 2):
    """Get price history for a product over the last N days"""
    from datetime import timedelta
    
    pid = pid.strip().upper()
    
    # Get product info
    product = _col_pids.find_one({"pid": pid}, {"_id": 0, "name": 1, "url": 1})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Calculate cutoff date
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    # Query price history
    records = list(_col.find(
        {"pid": pid, "ts": {"$gte": cutoff}},
        {"_id": 0, "ts": 1, "ts_last": 1, "fsp": 1, "nep": 1}
    ).sort("ts", ASCENDING))
    
    # Format records for display. MongoDB returns naive datetimes for values
    # stored as datetime.now(timezone.utc) — attach the UTC marker explicitly
    # so the browser doesn't misinterpret them as local time.
    def _utc_iso(dt):
        if not hasattr(dt, "isoformat"):
            return str(dt)
        if dt.tzinfo is None:
            return dt.isoformat() + "+00:00"
        return dt.isoformat()

    formatted_records = []
    for rec in records:
        formatted_records.append({
            "ts": _utc_iso(rec["ts"]),
            "ts_last": _utc_iso(rec["ts_last"]) if rec.get("ts_last") else None,
            "fsp": rec.get("fsp"),
            "nep": rec.get("nep"),
        })
    
    return {
        "pid": pid,
        "name": product.get("name", pid),
        "url": product.get("url", ""),
        "days": days,
        "count": len(formatted_records),
        "history": formatted_records
    }


@app.get("/stream")
async def stream():
    import asyncio
    q = asyncio.Queue(maxsize=200)
    with _lock:
        _clients.append(q)
    for snap in list(_state.values()):
        try:
            q.put_nowait(json.dumps({"type": "product", "data": snap}))
        except asyncio.QueueFull:
            break

    async def generator():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _lock:
                if q in _clients:
                    _clients.remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ── Admin Dashboard ───────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    """Main admin dashboard with stats, controls, and logs"""
    uptime = datetime.now() - _app_start_time
    uptime_str = str(uptime).split('.')[0]  # Remove microseconds
    
    product_count = len(load_products())
    display_scraper = _get_display_scraper()
    browser_status = "Active" if display_scraper else "Not initialized"
    login_status = "Unknown"
    if display_scraper and hasattr(display_scraper, 'account_name'):
        login_status = display_scraper.account_name or "Not logged in"
    
    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard - Flipkart Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #fff;
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        h1 {{
            font-size: 2rem;
            margin-bottom: 30px;
            text-align: center;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid rgba(255,255,255,0.2);
        }}
        .stat-label {{
            font-size: 0.9rem;
            opacity: 0.8;
            margin-bottom: 8px;
        }}
        .stat-value {{
            font-size: 1.8rem;
            font-weight: bold;
        }}
        .controls {{
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 30px;
            border: 1px solid rgba(255,255,255,0.2);
        }}
        .controls h2 {{
            font-size: 1.3rem;
            margin-bottom: 20px;
        }}
        .button-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }}
        .btn {{
            padding: 14px 24px;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            background: rgba(255,255,255,0.2);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
        }}
        .btn:hover {{
            background: rgba(255,255,255,0.3);
            transform: translateY(-2px);
        }}
        .btn-danger {{
            background: rgba(239, 68, 68, 0.8);
            border-color: rgba(239, 68, 68, 0.5);
        }}
        .btn-danger:hover {{
            background: rgba(239, 68, 68, 1);
        }}
        .logs-section {{
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 25px;
            border: 1px solid rgba(255,255,255,0.2);
        }}
        .logs-section h2 {{
            font-size: 1.3rem;
            margin-bottom: 15px;
        }}
        .logs-container {{
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            padding: 15px;
            height: 400px;
            overflow-y: auto;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 0.85rem;
            line-height: 1.6;
        }}
        .log-line {{
            margin-bottom: 5px;
            opacity: 0.9;
        }}
        .status-badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.85rem;
            background: rgba(34, 197, 94, 0.2);
            border: 1px solid rgba(34, 197, 94, 0.5);
        }}
        .status-badge.inactive {{
            background: rgba(239, 68, 68, 0.2);
            border-color: rgba(239, 68, 68, 0.5);
        }}
        .toast {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: rgba(0,0,0,0.9);
            color: white;
            padding: 15px 25px;
            border-radius: 8px;
            display: none;
            z-index: 1000;
        }}
        .toast.show {{
            display: block;
            animation: slideIn 0.3s ease-out;
        }}
        @keyframes slideIn {{
            from {{ transform: translateX(400px); opacity: 0; }}
            to {{ transform: translateX(0); opacity: 1; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🛠️ Admin Dashboard</h1>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">App Uptime</div>
                <div class="stat-value">{uptime_str}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Last Restarted</div>
                <div class="stat-value">{_app_start_time.strftime('%H:%M:%S')}</div>
                <div class="stat-label" style="font-size:0.75rem;margin-top:4px">{_app_start_time.strftime('%Y-%m-%d')}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Products Tracked</div>
                <div class="stat-value">{product_count}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Browser Status</div>
                <div class="stat-value" style="font-size:1.2rem">
                    <span class="status-badge {'' if display_scraper else 'inactive'}">{browser_status}</span>
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Login Status</div>
                <div class="stat-value" style="font-size:1.3rem">{login_status}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Auto-Restart</div>
                <div class="stat-value" style="font-size:1.2rem">
                    <span class="status-badge {'' if _auto_restart_enabled else 'inactive'}">{'Enabled' if _auto_restart_enabled else 'Disabled'}</span>
                </div>
            </div>
        </div>

        <div class="controls">
            <h2>⚙️ Controls</h2>
            <div class="button-grid">
                <button class="btn" onclick="reloadBrowser()">🔄 Reload Browser</button>
                <button class="btn" onclick="toggleAutoRestart()">⏰ Toggle Auto-Restart</button>
                <button class="btn" onclick="window.location.href='/admin/logs'">📋 Detailed Logs</button>
                <button class="btn" onclick="window.location.href='/'">📊 View Dashboard</button>
                <button class="btn btn-danger" onclick="restartApp()">🔴 Restart App</button>
            </div>
        </div>

        <div class="logs-section" style="margin-bottom: 20px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;">
                <h2 style="margin:0;">📋 Activity Logs <span style="font-size:0.9rem;opacity:0.7">(last 100 events)</span></h2>
                <button class="btn" onclick="refreshActivityLogs()" style="padding:8px 16px;font-size:0.85rem;">🔄 Refresh</button>
            </div>
            <div class="logs-container" id="activity-logs">
                <div class="log-line" style="opacity:0.5">Loading activity logs...</div>
            </div>
        </div>

        <div class="logs-section">
            <h2>📝 Live Logs <span style="font-size:0.9rem;opacity:0.7">(last 50 lines)</span></h2>
            <div class="logs-container" id="logs">
                <div class="log-line" style="opacity:0.5">Connecting to log stream...</div>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        function showToast(message) {{
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }}

        async function reloadBrowser() {{
            try {{
                const response = await fetch('/reload-browser', {{ method: 'POST' }});
                const data = await response.json();
                showToast(data.message);
            }} catch (e) {{
                showToast('Failed to reload browser');
            }}
        }}

        async function toggleAutoRestart() {{
            try {{
                const response = await fetch('/admin/toggle-auto-restart', {{ method: 'POST' }});
                const data = await response.json();
                showToast(data.message);
                setTimeout(() => location.reload(), 1500);
            }} catch (e) {{
                showToast('Failed to toggle auto-restart');
            }}
        }}

        async function restartApp() {{
            if (!confirm('Are you sure you want to restart the app? It will be back online in ~10 seconds.')) return;
            try {{
                const response = await fetch('/admin/restart', {{ method: 'POST' }});
                const data = await response.json();
                showToast(data.message);
            }} catch (e) {{
                showToast('Restart triggered - app restarting...');
            }}
        }}

        // Live log streaming
        const logsDiv = document.getElementById('logs');
        const eventSource = new EventSource('/admin/logs/stream');
        let logLines = [];
        
        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}
        
        eventSource.onmessage = function(event) {{
            const data = JSON.parse(event.data);
            if (data.line) {{
                logLines.push(data.line);
                if (logLines.length > 50) logLines.shift();
                logsDiv.innerHTML = logLines.map(line => `<div class="log-line">${{escapeHtml(line)}}</div>`).join('');
                logsDiv.scrollTop = logsDiv.scrollHeight;
            }}
        }};

        eventSource.onerror = function() {{
            logsDiv.innerHTML = '<div class="log-line" style="color:#ef4444">Log stream disconnected. Refresh to reconnect.</div>';
        }};

        // Activity logs loading
        async function refreshActivityLogs() {{
            try {{
                const res = await fetch('/activity-logs?limit=100');
                const data = await res.json();
                const container = document.getElementById('activity-logs');
                
                if (data.logs && data.logs.length > 0) {{
                    const formattedLogs = data.logs.map(line => {{
                        let color = 'rgba(255,255,255,0.7)';
                        let icon = '';
                        
                        if (line.includes('[PRODUCT ADD]')) {{
                            color = '#4ade80';
                            icon = '➕ ';
                        }} else if (line.includes('[PRODUCT EDIT]')) {{
                            color = '#fbbf24';
                            icon = '✏️ ';
                        }} else if (line.includes('[PRODUCT DELETE]')) {{
                            color = '#f87171';
                            icon = '🗑️ ';
                        }} else if (line.includes('[TELEGRAM ALERT]')) {{
                            color = '#60a5fa';
                            icon = '📢 ';
                        }} else if (line.includes('[HEALTH CHECK]')) {{
                            color = '#a78bfa';
                            icon = '💚 ';
                        }} else if (line.includes('ERROR')) {{
                            color = '#ef4444';
                            icon = '❌ ';
                        }}
                        
                        return `<div class="log-line" style="color: ${{color}};">${{icon}}${{escapeHtml(line)}}</div>`;
                    }}).join('');
                    
                    container.innerHTML = formattedLogs;
                    container.scrollTop = container.scrollHeight;
                }} else {{
                    container.innerHTML = '<div class="log-line" style="opacity:0.5">No activity logs found</div>';
                }}
            }} catch (err) {{
                console.error('Failed to fetch activity logs:', err);
                document.getElementById('activity-logs').innerHTML = '<div class="log-line" style="color:#ef4444">Error loading activity logs</div>';
            }}
        }}

        // Load activity logs on page load and refresh every 30 seconds
        refreshActivityLogs();
        setInterval(refreshActivityLogs, 30000);
    </script>
</body>
</html>
    """


# ── Admin Logs Endpoint ───────────────────────────────────────────────────────
@app.get("/admin/logs", response_class=HTMLResponse)
def admin_logs_page():
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin - Live Logs</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Consolas', 'Monaco', monospace;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid #30363d;
        }
        h1 {
            font-size: 24px;
            color: #58a6ff;
        }
        .controls {
            display: flex;
            gap: 10px;
        }
        button {
            background: #238636;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.2s;
        }
        button:hover {
            background: #2ea043;
        }
        .restart-btn {
            background: #da3633;
        }
        .restart-btn:hover {
            background: #f85149;
        }
        #status {
            padding: 8px 12px;
            background: #1f6feb;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }
        #logs {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 15px;
            height: calc(100vh - 140px);
            overflow-y: auto;
            font-size: 13px;
            line-height: 1.6;
        }
        .log-line {
            padding: 2px 0;
            border-bottom: 1px solid #21262d;
        }
        .log-line:last-child {
            border-bottom: none;
        }
        .timestamp {
            color: #8b949e;
            margin-right: 8px;
        }
        #logs::-webkit-scrollbar {
            width: 10px;
        }
        #logs::-webkit-scrollbar-track {
            background: #0d1117;
        }
        #logs::-webkit-scrollbar-thumb {
            background: #30363d;
            border-radius: 5px;
        }
        #logs::-webkit-scrollbar-thumb:hover {
            background: #484f58;
        }
        .confirmation {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: #161b22;
            border: 2px solid #30363d;
            border-radius: 8px;
            padding: 30px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
            display: none;
            z-index: 1000;
        }
        .confirmation.show {
            display: block;
        }
        .confirmation h2 {
            color: #f85149;
            margin-bottom: 15px;
        }
        .confirmation p {
            margin-bottom: 20px;
            color: #8b949e;
        }
        .confirmation .buttons {
            display: flex;
            gap: 10px;
            justify-content: flex-end;
        }
        .cancel-btn {
            background: #6e7681;
        }
        .cancel-btn:hover {
            background: #8b949e;
        }
        .overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.7);
            display: none;
            z-index: 999;
        }
        .overlay.show {
            display: block;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🔧 Admin - Live Logs</h1>
        <div class="controls">
            <span id="status">● Connected</span>
            <button onclick="clearLogs()">Clear Logs</button>
            <button class="restart-btn" onclick="showRestartConfirm()">🔄 Restart App</button>
        </div>
    </div>
    <div id="logs"></div>
    
    <div class="overlay" id="overlay" onclick="hideRestartConfirm()"></div>
    <div class="confirmation" id="confirmDialog">
        <h2>⚠️ Restart Application?</h2>
        <p>This will kill the current process and restart the app using start-app-ngrok.ps1</p>
        <div class="buttons">
            <button class="cancel-btn" onclick="hideRestartConfirm()">Cancel</button>
            <button class="restart-btn" onclick="confirmRestart()">Yes, Restart Now</button>
        </div>
    </div>

    <script>
        const logsDiv = document.getElementById('logs');
        const statusSpan = document.getElementById('status');
        let eventSource;
        let autoScroll = true;

        // Auto-scroll detection
        logsDiv.addEventListener('scroll', () => {
            const isAtBottom = logsDiv.scrollHeight - logsDiv.scrollTop <= logsDiv.clientHeight + 50;
            autoScroll = isAtBottom;
        });

        function connectLogs() {
            eventSource = new EventSource('/admin/logs/stream');
            
            eventSource.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.line) {
                    const line = document.createElement('div');
                    line.className = 'log-line';
                    line.textContent = data.line;
                    logsDiv.appendChild(line);
                    
                    // Keep only last 200 lines in DOM
                    while (logsDiv.children.length > 200) {
                        logsDiv.removeChild(logsDiv.firstChild);
                    }
                    
                    if (autoScroll) {
                        logsDiv.scrollTop = logsDiv.scrollHeight;
                    }
                }
            };
            
            eventSource.onerror = () => {
                statusSpan.textContent = '● Reconnecting...';
                statusSpan.style.background = '#f85149';
                setTimeout(() => {
                    eventSource.close();
                    connectLogs();
                }, 2000);
            };
            
            eventSource.onopen = () => {
                statusSpan.textContent = '● Connected';
                statusSpan.style.background = '#1f6feb';
            };
        }

        function clearLogs() {
            logsDiv.innerHTML = '';
        }

        function showRestartConfirm() {
            document.getElementById('overlay').classList.add('show');
            document.getElementById('confirmDialog').classList.add('show');
        }

        function hideRestartConfirm() {
            document.getElementById('overlay').classList.remove('show');
            document.getElementById('confirmDialog').classList.remove('show');
        }

        async function confirmRestart() {
            hideRestartConfirm();
            try {
                const response = await fetch('/admin/restart', { method: 'POST' });
                const data = await response.json();
                alert(data.message || 'Restart triggered!');
            } catch (error) {
                alert('Error triggering restart: ' + error.message);
            }
        }

        // Start connection
        connectLogs();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.get("/admin/logs/stream")
async def admin_logs_stream():
    import asyncio
    async def event_stream():
        q = asyncio.Queue()
        _log_clients.append(q)
        
        # Send existing logs first
        for log_line in _logs:
            yield f"data: {{\"line\": {json.dumps(log_line)}}}\n\n"
        
        try:
            while True:
                try:
                    log_line = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {{\"line\": {json.dumps(log_line)}}}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"  # SSE keepalive
        except asyncio.CancelledError:
            pass
        finally:
            if q in _log_clients:
                _log_clients.remove(q)
    
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/admin/restart")
def admin_restart():
    """Trigger app restart"""
    try:
        import platform
        # Log the restart
        logger.info("🔄 RESTART TRIGGERED via admin endpoint")
        
        if platform.system() == "Windows":
            # Execute restart script in background
            import subprocess
            script_path = r"c:\Users\Vasu\Desktop\Freelancer\flipkart-screen\start-app-ngrok.ps1"
            subprocess.Popen(
                ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        
        # Exit this process after a short delay (Railway will auto-restart)
        def delayed_exit():
            time.sleep(2)
            os._exit(0)
        
        threading.Thread(target=delayed_exit, daemon=True).start()
        
        return {"ok": True, "message": "Restart triggered! App will restart in 2 seconds."}
    except Exception as e:
        logger.error(f"Restart failed: {e}")
        return {"ok": False, "message": f"Restart failed: {str(e)}"}


@app.post("/admin/toggle-auto-restart")
def toggle_auto_restart():
    """Toggle automatic 2-hour restart"""
    global _auto_restart_enabled
    _auto_restart_enabled = not _auto_restart_enabled
    status = "enabled" if _auto_restart_enabled else "disabled"
    logger.info(f"⏰ Auto-restart {status}")
    return {"ok": True, "message": f"Auto-restart {status}", "enabled": _auto_restart_enabled}


# ── Auto-restart timer ────────────────────────────────────────────────────────
def health_check_loop():
    """Log system health every 30 minutes"""
    time.sleep(60)  # Wait 1 minute after startup before first check
    while True:
        try:
            # Count products
            total_products = len(_state)
            active_products = sum(1 for p in _state.values() if p.get('status') == 'OK')
            
            # Count alerts in last hour
            one_hour_ago = time.time() - 3600
            recent_alerts = sum(1 for a in _alerts if a.get('timestamp', 0) > one_hour_ago) if _alerts else 0
            
            # Uptime
            uptime_seconds = (datetime.now() - _app_start_time).total_seconds()
            uptime_hours = uptime_seconds / 3600
            
            # Log health status to activity log
            activity_logger.info("="*60)
            activity_logger.info(f"[HEALTH CHECK] Uptime: {uptime_hours:.1f}h | Products: {active_products}/{total_products} active | Alerts (1h): {recent_alerts}")
            activity_logger.info(f"[HEALTH CHECK] Scraper: {'Running' if _get_display_scraper() else 'Not initialized'} | Auto-restart: {'Enabled' if _auto_restart_enabled else 'Disabled'}")
            activity_logger.info("="*60)
        except Exception as e:
            logger.error(f"[HEALTH CHECK] Error: {e}")
        
        time.sleep(1800)  # 30 minutes


def auto_restart_timer():
    """Restart app every 1 hour if enabled"""
    while True:
        time.sleep(300)  # Check every 5 minutes
        if _auto_restart_enabled:
            uptime = (datetime.now() - _app_start_time).total_seconds()
            if uptime >= 3600:  # 1 hour = 3600 seconds
                logger.info("⏰ Auto-restart triggered (1 hour uptime)")
                import platform
                if platform.system() == "Windows":
                    # Windows: use PowerShell script
                    import subprocess
                    script_path = r"c:\Users\Vasu\Desktop\Freelancer\flipkart-screen\start-app-ngrok.ps1"
                    subprocess.Popen(
                        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script_path],
                        creationflags=subprocess.CREATE_NEW_CONSOLE
                    )
                    time.sleep(2)
                # Linux/Railway: just exit, let Railway restart policy handle it
                os._exit(0)
 