import os
import re
import json
import time
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional, List
import requests
from bs4 import BeautifulSoup
from email.message import EmailMessage
import smtplib
from dotenv import load_dotenv

# -------- Config --------

@dataclass
class Config:
    url: str
    poll_seconds: int
    state_file: str

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_use_tls: bool
    from_email: str
    to_emails: List[str]
    subject_prefix: str

def load_config() -> Config:
    load_dotenv()
    return Config(
        url=os.getenv("TARGET_URL", "https://bransch.trafikverket.se/for-dig-i-branschen/jarnvag/Underlag-till-linjebok/Andringar-i-linjebok/"),
        poll_seconds=int(os.getenv("POLL_SECONDS", "600")),
        state_file=os.getenv("STATE_FILE", "page_state.json"),

        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_pass=os.getenv("SMTP_PASS", ""),
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "true").lower() in ("1","true","yes"),
        from_email=os.getenv("FROM_EMAIL", ""),
        to_emails=[e.strip() for e in os.getenv("TO_EMAILS","").split(",") if e.strip()],
        subject_prefix=os.getenv("SUBJECT_PREFIX","[Linjebok Watch]"),
    )

# -------- State --------

def load_last_seen(path: str) -> Optional[List[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("last_seen_changes")
    except FileNotFoundError:
        return None
    except Exception as e:
        logging.warning("Could not read state file %s: %s", path, e)
        return None

def save_last_seen(path: str, entries: List[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_seen_changes": entries}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# -------- Fetch & parse --------

UA = "Mozilla/5.0 (compatible; LinjebokUpdatedateWatcher/1.0)"

def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text

def extract_latest_changes(html: str) -> List[str]:
    """
    Extracts a list of text entries from the 'Senaste publicerade ändringar' section.
    Example return:
    ["2025-10-26 Stockholm, versionsändring",
     "2025-10-17 Ånge, versionsändring",
     "2025-10-12 Göteborg, versionsändring"]
    """
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("h3", string=re.compile("Senaste publicerade ändringar", re.I))
    if not header:
        raise RuntimeError("Could not find 'Senaste publicerade ändringar' section.")

    p = header.find_next("p")
    if not p:
        raise RuntimeError("Could not find paragraph following the header.")

    text = p.decode_contents().replace("&nbsp;", " ")
    # Break entries by <br> tags or newlines
    parts = re.split(r"<br\s*/?>", text)
    entries = []
    for part in parts:
        # Strip HTML tags and normalize spaces
        clean = re.sub(r"<.*?>", "", part).strip()
        if clean:
            entries.append(clean)
    return entries


# -------- Email --------

def send_email(cfg: Config, subject_suffix: str, body: str) -> None:
    if not (cfg.smtp_user and cfg.smtp_pass and cfg.from_email and cfg.to_emails):
        raise RuntimeError("SMTP or email addresses not configured. Check your .env.")
    msg = EmailMessage()
    msg["From"] = cfg.from_email
    msg["To"] = ", ".join(cfg.to_emails)
    msg["Subject"] = f"{cfg.subject_prefix} {subject_suffix}"
    msg.set_content(body)

    if cfg.smtp_use_tls:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)

# -------- Main check --------

def check_once(cfg: Config, last_seen: Optional[List[str]]) -> List[str]:
    html = fetch_html(cfg.url)
    entries = extract_latest_changes(html)

    if not entries:
        logging.warning("No entries found in change list.")
        return last_seen or []

    if last_seen is None:
        # First run: initialize
        save_last_seen(cfg.state_file, entries)
        logging.info("Initialized with %d entries (latest: %s)", len(entries), entries[0])
        return entries

    # Identify new entries (usually appear at the top)
    new_entries = [e for e in entries if e not in last_seen]

    if new_entries:
        old_first = last_seen[0] if last_seen else "(ingen tidligere oppføring)"
        new_first = entries[0]

        body = (
            f"Nye endringer oppdaget på siden.\n\n"
            f"Nye oppføringer ({len(new_entries)}):\n"
            + "\n".join(f"- {e}" for e in new_entries)
            + "\n\n"
            f"Tidligere første oppføring:\n{old_first}\n"
            f"Nyeste første oppføring:\n{new_first}\n\n"
            f"URL: {cfg.url}\n"
        )

        send_email(cfg, subject_suffix=f"{len(new_entries)} nye endringer (nyeste: {new_first.split()[0]})", body=body)
        logging.info("Emailed %d new entries (latest %s).", len(new_entries), new_first)
        save_last_seen(cfg.state_file, entries)
        return entries

    logging.info("Ingen nye endringer siden sist (%s)", entries[0])
    return last_seen

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    logging.info("Watching %s", cfg.url)

    last_seen = load_last_seen(cfg.state_file)

    # Bootstrap: initialize with current page contents (no email)
    if last_seen is None:
        html = fetch_html(cfg.url)
        entries = extract_latest_changes(html)
        save_last_seen(cfg.state_file, entries)
        logging.info("Initialized last_seen with %d entries (latest: %s)", len(entries), entries[0])
        last_seen = entries

    try:
        while True:
            try:
                last_seen = check_once(cfg, last_seen)
            except Exception as e:
                logging.exception("Check failed: %s", e)
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Stopped by user.")


if __name__ == "__main__":
    main()
