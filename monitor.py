"""
IMAX Ticket Monitor
Polls a cinema ticketing page and alerts you when a target movie appears.
Supports: Event Cinemas (Sydney IMAX), Village, or any custom URL.
Alerts via: desktop notification + optional email.
"""
import sys
import schedule
import time
import smtplib
import logging
import hashlib
import json
import os
from email.mime.text import MIMEText
from datetime import datetime

import requests
from bs4 import BeautifulSoup

day = "wednesday"
day_time = "16:12"


try:
    from plyer import notification as desktop_notify
    DESKTOP_NOTIFY_AVAILABLE = True
except ImportError:
    DESKTOP_NOTIFY_AVAILABLE = False

# 
# CONFIG — edit these before running
# 
CONFIG = {
    "target_movie": "odyssey",
    "url": "https://www.eventcinemas.com.au/cinema/IMAX-Sydney",
    "poll_interval_seconds": 300,
    "email_enabled": True,
    "email_from": os.environ.get("EMAIL_ADDRESS", ""),
    "email_to": os.environ.get("EMAIL_ADDRESS1", ""),
    "email_password": os.environ.get("EMAIL_PASSWORD", ""),
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "desktop_notify_enabled": True,
    "state_file": "monitor_state.json",
}
# 

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── State helpers 

def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"alerted_hashes": []}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ── Scraper

def fetch_page(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning(f"Fetch failed: {e}")
        return None


def find_movie(html: str, target: str) -> list[dict]:
    """
    Parse the page and return any sessions/listings that match the target movie.
    Returns a list of dicts with 'title' and 'details' keys.

    This generic parser looks for common patterns across cinema sites.
    You may need to tweak the CSS selectors for your specific site.
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    target_lower = target.lower()

    # Strategy 1: look for elements whose text contains the movie title
    # Common selectors used by Event Cinemas, Hoyts, Village
    candidate_selectors = [
        "h2", "h3", "h4",
        ".movie-title", ".film-title", ".session-title",
        "[class*='movie']", "[class*='film']", "[class*='session']",
        "a[href*='movie']", "a[href*='film']",
    ]

    seen = set()
    for selector in candidate_selectors:
        for el in soup.select(selector):
            text = el.get_text(strip=True)
            if target_lower in text.lower() and text not in seen:
                seen.add(text)
                # Try to grab nearby context (date, time, booking link)
                parent = el.find_parent()
                details = parent.get_text(" | ", strip=True) if parent else text
                matches.append({"title": text, "details": details[:300]})

    return matches


# ── Alerts 

def send_desktop_notification(movie: str, details: str) -> None:
    if not DESKTOP_NOTIFY_AVAILABLE:
        log.info("plyer not installed — skipping desktop notification.")
        return
    try:
        desktop_notify.notify(
            title=f"🎬 IMAX tickets available: {movie}",
            message=details[:200],
            app_name="IMAX Monitor",
            timeout=10,
        )
        log.info("Desktop notification sent.")
    except Exception as e:
        log.warning(f"Desktop notification failed: {e}")


def send_email(cfg: dict, movie: str, details: str, found: bool = True) -> None:
    if found:
        subject = f"🎬 IMAX tickets on sale: {movie}"
        body = (
            f"Your target movie '{movie}' is now showing on IMAX!\n\n"
            f"Details:\n{details}\n\n"
            f"Book now: {cfg['url']}\n\n"
            f"Detected at: {datetime.now().strftime('%d %b %Y %H:%M:%S')}"
        )
    else:
        subject = f"❌ IMAX Monitor — {movie} not yet available"
        body = (
            f"'{movie}' is not showing on IMAX Sydney yet.\n\n"
            f"Will check again next Tuesday.\n\n"
            f"Checked at: {datetime.now().strftime('%d %b %Y %H:%M:%S')}"
        )
    
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg["email_from"]
    msg["To"] = cfg["email_to"]

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["email_from"], cfg["email_password"])
            server.send_message(msg)
        log.info(f"Email alert sent to {cfg['email_to']}.")
    except Exception as e:
        log.error(f"Email send failed: {e}")

def alert(cfg: dict, movie: str, matches: list[dict]) -> None:
    details = "\n".join(m["details"] for m in matches)
    log.info(f"🎬 MATCH FOUND — {movie}\n{details}")

    if cfg["desktop_notify_enabled"]:
        send_desktop_notification(movie, details)

    if cfg["email_enabled"]:
        send_email(cfg, movie, details)


# ── Main loop

def check(cfg: dict, state: dict) -> None:
    log.info(f"Polling {cfg['url']} ...")
    html = fetch_page(cfg["url"])
    if html:
        matches = find_movie(html, cfg["target_movie"])
        if matches:
            alert(cfg, cfg["target_movie"], matches)
            state["alerted_hashes"].append(key)
            save_state(cfg["state_file"], state)
        else:
            log.info(f"'{cfg['target_movie']}' not found yet.")
            send_email(cfg, cfg["target_movie"], "", found=False)

if __name__ == "__main__":
    # Pull email password from environment variable if set (for GitHub Actions)
    if os.environ.get("EMAIL_PASSWORD"):
        CONFIG["email_password"] = os.environ["EMAIL_PASSWORD"]

    state = load_state(CONFIG["state_file"])

    # --once flag: check once and exit (used by GitHub Actions)
    if "--once" in sys.argv:
        log.info("Running single check...")
        check(CONFIG, state)
    else:
        schedule.every().day.at(day_time).do(check, cfg=CONFIG, state=state)
        log.info("Scheduler running — will check every " + day + " at " + day_time)
        while True:
            schedule.run_pending()
            time.sleep(60)
