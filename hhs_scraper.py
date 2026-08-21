#!/usr/bin/env python3
"""
CMS Risk Adjustment scraper (config-driven, GitHub Actions ready)
-----------------------------------------------------------------
All settings live in config.toml (scraping targets, CSV path, alert method).

Behaviour:
  1. Loads config.toml.
  2. Loads the existing CSV of already-seen files (if present).
  3. Scrapes the site for the current list of files.
  4. Keeps only the files NOT already in the CSV (new drops).
  5. Stamps each new file with the date it was found.
  6. Appends the new rows to the CSV.
  7. Alerts with details of ONLY the new files (Teams / SMTP / Outlook).

Run from a terminal:  python cms_scraper.py [path/to/config.toml]
On GitHub Actions:     python cms_scraper.py   (uses cloudscraper + Teams)
"""

import os
import re
import csv
import sys
import time
import tomllib
from datetime import date
from urllib.parse import urljoin

import requests
import cloudscraper          # survives CMS/Akamai 403 on datacenter IPs
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(path="config.toml"):
    if not os.path.exists(path):
        print(f"[ERROR] Config file not found: {path}")
        print(f"        Working directory: {os.getcwd()}")
        sys.exit(1)
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def get_html(session, url, cfg):
    """GET a URL with retries; raises on repeated failure."""
    s = cfg["scraping"]
    h = cfg["http"]
    headers = {
        "User-Agent": h["user_agent"],
        "Accept": h["accept"],
        "Accept-Language": h["accept_language"],
        "Upgrade-Insecure-Requests": "1",
    }
    retries = s["retries"]
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=headers, timeout=s["timeout"])
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  [warn] attempt {attempt}/{retries} failed for {url}: {e}")
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


def find_year_directories(html, cfg):
    """From the main page, return [(year, label, url), ...] for each directory
    link matching the configured year_link_regex."""
    base = cfg["scraping"]["base_url"]
    year_re = re.compile(cfg["scraping"]["year_link_regex"], re.IGNORECASE)
    soup = BeautifulSoup(html, "html.parser")
    dirs, seen = [], set()
    for a in soup.find_all("a", href=True):
        label = a.get_text(strip=True)
        m = year_re.match(label)
        if m:
            url = urljoin(base, a["href"])
            if url not in seen:
                seen.add(url)
                dirs.append((m.group(1), label, url))
    dirs.sort(key=lambda x: x[0], reverse=True)  # newest first
    return dirs


def _record(a, page_url, exts):
    """Build a file record from an <a> tag, or None if it isn't a file link."""
    href = a["href"]
    name = a.get_text(strip=True)
    if not name:
        return None
    low = href.lower()
    is_file = low.endswith(tuple(exts)) or "/files/" in low or "/document/" in low
    if not is_file:
        return None
    ext = next((e for e in exts if low.endswith(e)), "")
    return {"name": name, "url": urljoin(page_url, href), "ext": ext.lstrip(".")}


def scrape_downloads(html, page_url, cfg):
    """From a year's page, return list of {name, url, ext} in the Downloads
    section. Falls back to all file-type links if no Downloads heading found."""
    exts = cfg["scraping"]["file_extensions"]
    soup = BeautifulSoup(html, "html.parser")
    recs, seen = [], set()

    # Prefer links under a "Downloads" heading, stopping at the next heading.
    heading = soup.find(lambda t: t.name in ("h2", "h3", "h4")
                        and "download" in t.get_text(strip=True).lower())
    if heading:
        stop_levels = {"h1", "h2", "h3", "h4"}
        for el in heading.find_all_next():
            if el.name in stop_levels and el is not heading:
                break
            if el.name == "a" and el.has_attr("href"):
                rec = _record(el, page_url, exts)
                if rec and rec["url"] not in seen:
                    seen.add(rec["url"])
                    recs.append(rec)
        if recs:
            return recs

    # Fallback: any file-type link anywhere on the page.
    for a in soup.find_all("a", href=True):
        rec = _record(a, page_url, exts)
        if rec and rec["url"] not in seen:
            seen.add(rec["url"])
            recs.append(rec)
    return recs


# ---------------------------------------------------------------------------
# CSV state
# ---------------------------------------------------------------------------
CSV_COLUMNS = ["year", "directory", "file_name", "file_ext",
               "file_url", "date_found"]


def _key(url, year, name):
    """Unique identity for a file: prefer URL, else year|name."""
    return url.strip() if url and url.strip() else f"{year}|{name}".strip()


def load_existing(csv_file):
    """Return (rows, seen_keys) from the existing CSV."""
    rows, keys = [], set()
    if not os.path.exists(csv_file):
        return rows, keys
    with open(csv_file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
            keys.add(_key(row["file_url"], row["year"], row["file_name"]))
    return rows, keys


# ---------------------------------------------------------------------------
# Alerts (Teams / SMTP / Outlook)
# ---------------------------------------------------------------------------
def build_alert_body(new_rows, cfg):
    """Plain-text body listing ONLY the new files, grouped by year."""
    lines = [f"{len(new_rows)} new file(s) posted on the CMS Risk Adjustment page.",
             ""]
    current_year = None
    for r in sorted(new_rows, key=lambda x: (x["year"], x["file_name"]),
                    reverse=True):
        if r["year"] != current_year:
            current_year = r["year"]
            lines.append(f"{r['directory']}:")
        lines.append(f"  - {r['file_name']} "
                     f"({r['file_ext'] or 'file'})  |  found {r['date_found']}")
        lines.append(f"    {r['file_url']}")
    lines += ["", "Page: " + cfg["scraping"]["main_url"]]
    return "\n".join(lines)


def send_teams(subject, body, cfg):
    """Post an alert to a Teams chat/channel via a Workflows Incoming Webhook.
    Webhook URL comes from env var TEAMS_WEBHOOK_URL (GitHub Secret)."""
    url = os.environ["TEAMS_WEBHOOK_URL"]
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": subject,
                     "weight": "Bolder", "size": "Medium", "wrap": True},
                    {"type": "TextBlock", "text": body, "wrap": True},
                ],
            },
        }],
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def send_outlook(subject, body, cfg):
    """Local, zero-setup: sends through your open Outlook as you.
    Requires: pip install pywin32  (and Outlook running). Not used on GitHub."""
    import win32com.client
    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)
    mail.To = "; ".join(cfg["alert"]["to"])
    mail.Subject = subject
    mail.Body = body
    mail.Send()


def send_smtp(subject, body, cfg):
    """Internet-reachable SMTP (SendGrid/Brevo). Credentials from env vars
    SMTP_USER / SMTP_PASS. Not used in the Teams setup."""
    import smtplib
    from email.message import EmailMessage
    sm = cfg["alert"]["smtp"]
    msg = EmailMessage()
    msg["From"] = sm["from_address"]
    msg["To"] = ", ".join(cfg["alert"]["to"])
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(sm["host"], sm["port"]) as srv:
        if sm.get("use_tls"):
            srv.starttls()
        if sm.get("use_auth"):
            srv.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        srv.send_message(msg)


def send_alert(subject, body, cfg):
    """Dispatch to the configured sender; never let an alert error lose the CSV."""
    if not cfg["alert"].get("enabled", True):
        print("      Alert disabled in config; skipping.")
        return
    senders = {"teams": send_teams, "smtp": send_smtp, "outlook": send_outlook}
    via = cfg["alert"]["send_via"]
    try:
        sender_fn = senders[via]        # look up the function first
        sender_fn(subject, body, cfg)   # then call it
        print(f"      Alert sent via '{via}'.")
    except Exception as e:
        print(f"      [warn] alert not sent ({e}). CSV was still updated.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(config_path="config.toml"):
    # Ignore any injected args; honor a real config path only if it exists.
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
            and os.path.exists(sys.argv[1]):
        config_path = sys.argv[1]

    cfg = load_config(config_path)
    csv_file = cfg["storage"]["csv_file"]
    main_url = cfg["scraping"]["main_url"]

    # cloudscraper mimics a real browser TLS/JS challenge -> avoids Akamai 403.
    # Exposes the same .get() API as requests.Session().
    session = cloudscraper.create_scraper()

    # 1) Load what we already know
    existing_rows, seen_keys = load_existing(csv_file)
    print(f"[1/4] Existing CSV: {len(existing_rows)} file(s) already recorded.")

    # 2) Scrape current state of the site
    print(f"[2/4] Fetching main page: {main_url}")
    try:
        main_html = get_html(session, main_url, cfg)
    except requests.RequestException as e:
        print(f"[ERROR] Could not load main page: {e}")
        print("If this is a 403, CMS/Akamai is blocking the request. "
              "cloudscraper usually handles it; if not, try a Playwright fetch.")
        sys.exit(1)

    dirs = find_year_directories(main_html, cfg)
    print(f"      Found {len(dirs)} year directories: "
          f"{', '.join(y for y, _, _ in dirs)}")

    scraped = []
    for i, (year, label, url) in enumerate(dirs, 1):
        print(f"      ({i}/{len(dirs)}) {label}")
        try:
            files = scrape_downloads(get_html(session, url, cfg), url, cfg)
        except requests.RequestException as e:
            print(f"        [warn] skipped ({e})")
            files = []
        for fl in files:
            scraped.append({"year": year, "directory": label,
                            "file_name": fl["name"], "file_ext": fl["ext"],
                            "file_url": fl["url"]})
        time.sleep(cfg["scraping"]["request_delay"])  # be polite

    # 3) Keep only files not already in the CSV
    today = date.today().isoformat()
    new_rows = []
    for r in scraped:
        k = _key(r["file_url"], r["year"], r["file_name"])
        if k not in seen_keys:
            seen_keys.add(k)  # guard against dupes within this run
            r["date_found"] = today
            new_rows.append(r)

    print(f"[3/4] New files found: {len(new_rows)}")
    for r in new_rows:
        print(f"        + [{r['year']}] {r['file_name']} ({r['date_found']})")

    # 4) Append new rows to the CSV + alert with ONLY the new files
    if new_rows:
        write_header = not os.path.exists(csv_file)
        with open(csv_file, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if write_header:
                w.writeheader()
            for r in new_rows:
                w.writerow(r)
        print(f"[4/4] Appended {len(new_rows)} new row(s) to {csv_file}.")
        send_alert(cfg["alert"]["subject"], build_alert_body(new_rows, cfg), cfg)
    else:
        print(f"[4/4] No new files. {csv_file} left unchanged. No alert sent.")


if __name__ == "__main__":
    main()
