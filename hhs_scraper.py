#!/usr/bin/env python3
"""
HHS Risk Adjustment DIY Software scraper (config-driven, GitHub Actions ready)
-----------------------------------------------------------------------------
Watches the CMS Marketplace "Regulations and Guidance" page for every dated
HHS-Developed Risk Adjustment Model Algorithm "Do It Yourself (DIY)" Software
release and alerts when a new one is posted.

Behaviour (identical to the CMS watcher, only the SCRAPING logic differs):
  1. Loads config.toml.
  2. Loads the existing CSV of already-seen files (if present).
  3. Scrapes the page for DIY-software entries (single-stage, keyword match).
  4. Keeps only the entries NOT already in the CSV (new drops).
  5. Stamps each new entry with the date it was found.
  6. Appends the new rows to the CSV.
  7. Alerts with details of ONLY the new entries (Teams / SMTP / Outlook).

Run from a terminal:  python hhs_scraper.py [path/to/config.toml]
On GitHub Actions:     python hhs_scraper.py   (uses cloudscraper + Teams)
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


def scrape_matching_entries(html, page_url, cfg):
    """Single-stage scrape for HHS DIY software.

    Finds every downloadable file link whose entry line matches the DIY
    keyword, cleans the title to "date + <NNNN Benefit Year ...> Software"
    (dropping trailing sub-items like Instructions/Technical/SAS), extracts
    the benefit year, and returns ONE record per dated entry.

    A single entry's <li> may hold several downloads (the DIY zip plus an
    Instructions PDF, a Technical XLSX and a SAS-version zip); we keep just
    one record per entry and prefer the DIY zip over the SAS zip.

    Returns list of {year, directory, file_name, file_ext, file_url}.
    """
    s = cfg["scraping"]
    exts = tuple(s["file_extensions"])
    keyword_re = re.compile(s["keyword_regex"], re.IGNORECASE)
    year_re = re.compile(s["year_regex"])
    directory_label = s.get("directory_label", "HHS DIY Software")

    soup = BeautifulSoup(html, "html.parser")
    by_title = {}   # cleaned_title -> record (dedupe per dated entry)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        is_file = low.endswith(exts) or "/files/" in low or "/document/" in low
        if not is_file:
            continue

        # The link's whole entry line (li/p/td) carries the date + heading.
        anchor_text = a.get_text(" ", strip=True)
        parent = a.find_parent(["li", "p", "td", "div"])
        context = parent.get_text(" ", strip=True) if parent else anchor_text
        haystack = f"{anchor_text} | {context}"

        # Only entries about the DIY software.
        if not keyword_re.search(haystack):
            continue

        raw = context if len(context) >= len(anchor_text) else anchor_text
        raw = " ".join(raw.split())

        # Keep only "date + <NNNN Benefit Year ... Do It Yourself (DIY)> Software"
        # and drop the trailing sub-items (Instructions/Technical/SAS/etc.).
        m = re.search(r"^(.*?(?:do it yourself|\bdiy\b).{0,60}?software)\b",
                      raw, re.IGNORECASE)
        title = (m.group(1) if m else raw).strip()

        # Benefit year: prefer the "NNNN Benefit Year" number over any other
        # 20xx (CMS may post a benefit year's software in a later calendar year).
        by = re.search(r"(20\d{2})\s+Benefit\s+Year", title, re.IGNORECASE)
        if by:
            year = by.group(1)
        else:
            ym = year_re.search(title) or year_re.search(haystack)
            year = ym.group(1) if ym else ""

        ext = next((e for e in exts if low.endswith(e)), "")
        url = urljoin(page_url, href)

        # Prefer the DIY download over a SAS-version download in the same entry.
        is_sas = ("sas" in low) or ("sas" in anchor_text.lower())
        score = 0 if is_sas else 1

        prev = by_title.get(title)
        if prev is None or score > prev["_score"]:
            by_title[title] = {"year": year, "directory": directory_label,
                               "file_name": title, "file_ext": ext.lstrip("."),
                               "file_url": url, "_score": score}

    recs = []
    for r in by_title.values():
        r.pop("_score", None)
        recs.append(r)
    return recs


# ---------------------------------------------------------------------------
# CSV state
# ---------------------------------------------------------------------------
CSV_COLUMNS = ["year", "directory", "file_name", "file_ext",
               "file_url", "date_found"]


def _key(url, year, name):
    """Unique identity for an entry: prefer URL, else year|name."""
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
    """Plain-text body listing ONLY the new entries - one separate block per
    dated release (multiple releases per benefit year are listed individually,
    never collapsed into a single per-year record)."""
    source = cfg["alert"].get("source_label", "the watched page")
    lines = [f"{len(new_rows)} new file(s) posted on {source}.", ""]
    for r in sorted(new_rows, key=lambda x: (x["year"], x["file_name"]),
                    reverse=True):
        lines.append(f"- {r['file_name']}")
        lines.append(f"    Benefit year: {r['year'] or 'n/a'}"
                     f"  |  {r['file_ext'] or 'file'}  |  found {r['date_found']}")
        lines.append(f"    {r['file_url']}")
        lines.append("")  # blank line between entries for readability
    lines += ["Page: " + cfg["scraping"]["main_url"]]
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
    session = cloudscraper.create_scraper()

    # 1) Load what we already know
    existing_rows, seen_keys = load_existing(csv_file)
    print(f"[1/4] Existing CSV: {len(existing_rows)} entry(ies) already recorded.")

    # 2) Scrape current state of the page (single stage)
    print(f"[2/4] Fetching page: {main_url}")
    try:
        html = get_html(session, main_url, cfg)
    except requests.RequestException as e:
        print(f"[ERROR] Could not load page: {e}")
        print("If this is a 403, CMS/Akamai is blocking the request. "
              "cloudscraper usually handles it; if not, try a Playwright fetch.")
        sys.exit(1)

    scraped = scrape_matching_entries(html, main_url, cfg)
    years = sorted({r["year"] for r in scraped if r["year"]}, reverse=True)
    print(f"      Matched {len(scraped)} DIY entry(ies) across years: "
          f"{', '.join(years) if years else '(none)'}")

    # 3) Keep only entries not already in the CSV
    today = date.today().isoformat()
    new_rows = []
    for r in scraped:
        k = _key(r["file_url"], r["year"], r["file_name"])
        if k not in seen_keys:
            seen_keys.add(k)  # guard against dupes within this run
            r["date_found"] = today
            new_rows.append(r)

    print(f"[3/4] New entries found: {len(new_rows)}")
    for r in new_rows:
        print(f"        + [{r['year']}] {r['file_name']} ({r['date_found']})")

    # 4) Append new rows to the CSV + alert with ONLY the new entries
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
        print(f"[4/4] No new entries. {csv_file} left unchanged. No alert sent.")


if __name__ == "__main__":
    main()
