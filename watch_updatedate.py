#!/usr/bin/env python3
import os
import sys
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# -------- App dir (works for PyInstaller .exe and normal Python) --------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# -------- Config --------
@dataclass
class Config:
    url: str
    poll_seconds: int
    state_file: str  # full path

def load_config() -> Config:
    load_dotenv()
    url = os.getenv(
        "TARGET_URL",
        "https://bransch.trafikverket.se/for-dig-i-branschen/jarnvag/Underlag-till-linjebok/Andringar-i-linjebok/",
    )
    poll_seconds = int(os.getenv("POLL_SECONDS", "600"))
    state_name = os.getenv("STATE_FILE", "page_state.json")
    state_file = state_name if os.path.isabs(state_name) else os.path.join(APP_DIR, state_name)
    return Config(url=url, poll_seconds=poll_seconds, state_file=state_file)

# -------- State (now stores last + previous) --------
def load_state(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        last = data.get("last_seen_updated_date")
        prev = data.get("previous_seen_updated_date")  # new key
        return last, prev
    except FileNotFoundError:
        return None, None
    except Exception as e:
        logging.warning("Could not read state file %s: %s", path, e)
        return None, None

def save_state(path: str, last_iso: str, prev_iso: Optional[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_seen_updated_date": last_iso,
                "previous_seen_updated_date": prev_iso,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(tmp, path)

# -------- Fetch & parse --------
UA = "Mozilla/5.0 (compatible; LinjebokUpdatedateWatcher/1.0)"
UPDATED_RE = re.compile(
    r"Senast\s+uppdaterad\s*/\s*granskad:\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE
)

def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text

def extract_updated_date_iso(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True).replace("\xa0", " ")
    m = UPDATED_RE.search(text)
    if not m:
        for ln in text.splitlines():
            if "Senast uppdaterad" in ln:
                m2 = re.search(r"(\d{4}-\d{2}-\d{2})", ln)
                if m2:
                    return m2.group(1)
        raise RuntimeError("Could not find 'Sist oppdatert' date on page.")
    return m.group(1)

# -------- Helpers --------
def fmt_ddmm(iso: Optional[str]) -> str:
    """Format 'YYYY-MM-DD' -> 'DD/MM' for compact logging."""
    if not iso:
        return "unknown"
    try:
        y, m, d = iso.split("-")
        return f"{int(d):02d}/{int(m):02d}"
    except Exception:
        return iso  # fallback to raw if unexpected

# -------- Main check --------
def check_once(cfg: Config, last_seen: Optional[str], prev_seen: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    html = fetch_html(cfg.url)
    iso = extract_updated_date_iso(html)

    if not last_seen or iso > last_seen:
        # Page shows a newer date -> rotate state (prev <- last, last <- iso)
        logging.info(
            "UPDATED: %s (prev %s). URL: %s",
            fmt_ddmm(iso),
            fmt_ddmm(last_seen),
            cfg.url,
        )
        save_state(cfg.state_file, iso, last_seen)
        return iso, last_seen
    else:
        # No change -> report both current last and previous
        logging.info(
            "No change, last updated %s, previous update %s.",
            fmt_ddmm(last_seen),
            fmt_ddmm(prev_seen),
        )
        return last_seen, prev_seen

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    logging.info("Watching: %s", cfg.url)
    logging.info("State file: %s", cfg.state_file)
    logging.info("Interval: %s seconds", cfg.poll_seconds)

    last_seen, prev_seen = load_state(cfg.state_file)

    # Bootstrap: initialize with current date; no "update" message on first run
    if last_seen is None:
        try:
            iso = extract_updated_date_iso(fetch_html(cfg.url))
            save_state(cfg.state_file, iso, None)
            last_seen, prev_seen = iso, None
            logging.info("Initialized last_seen to %s", iso)
        except Exception as e:
            logging.exception("Failed to initialize state: %s", e)

    try:
        while True:
            try:
                last_seen, prev_seen = check_once(cfg, last_seen, prev_seen)
            except Exception as e:
                logging.exception("Check failed: %s", e)
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Stopped by user.")

if __name__ == "__main__":
    main()
