#!/usr/bin/env python3
"""
HHS Risk Adjustment DIY Software scraper (config-driven, GitHub Actions ready)
-----------------------------------------------------------------------------
Watches the CMS Marketplace "Regulations and Guidance" page for every dated
HHS-Developed Risk Adjustment Model Algorithm "Do It Yourself (DIY)" Software
release and alerts when a new one is posted.

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


MONTHS = ("January|February|March|April|May|June|July|August|"
          "September|October|November|December")
DATE_RE = re.compile(rf"\b(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", re.IGNORECASE)


def _norm(text):
    """Normalize for matching: unify smart quotes, collapse spaces, lowercase."""
    text = (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))
    return re.sub(r"\s+", " ", text).strip().lower()


def scrape_matching_entries(html, page_url, cfg):
    """Single-stage scrape for HHS DIY software.

    The page lists each release as a DATE line followed by a HEADING line
    ("<NNNN> Benefit Year ... Do It Yourself (DIY) ... Software"), usually with
    sub-item download links (DIY zip, SAS zip, Instructions PDF, ...) nested
    underneath.  We:
      1. Scan the page TEXT line-by-line to capture EVERY dated DIY heading as
         its own record (multiple per benefit year are kept separately).
      2. Build a heading -> zip-URL map from the DOM (preferring the DIY zip
         over a SAS-version zip) and attach a URL to each record.

    Returns list of {year, directory, file_name, file_ext, file_url}.
    """
    s = cfg["scraping"]
    exts = tuple(s["file_extensions"])
    keyword_re = re.compile(s["keyword_regex"], re.IGNORECASE)
    directory_label = s.get("directory_label", "HHS DIY Software")

    soup = BeautifulSoup(html, "html.parser")

    # ---- (A) Map each DIY heading to its best zip URL (from the DOM) --------
    heading_url = {}   # normalized (date + heading) -> {"url":.., "sas":bool}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        is_file = low.endswith(exts) or "/files/" in low or "/document/" in low
        if not is_file:
            continue
        h = a.find_previous(string=keyword_re)   # nearest DIY heading text
        if not h:
            continue
        heading = DATE_RE.sub("", str(h)).strip(" -\u2013:\t")
        # Include the date preceding this heading so entries that share the
        # same heading text (e.g. two releases in the same benefit year) get
        # DISTINCT keys and keep their own zip URL.
        dnode = h.find_previous(string=DATE_RE)
        dmatch = DATE_RE.search(str(dnode)) if dnode else None
        date_part = dmatch.group(0) if dmatch else ""
        key = _norm(f"{date_part} {heading}")
        if not key:
            continue
        is_sas = ("sas" in low) or ("sas" in a.get_text(" ", strip=True).lower())
        cur = heading_url.get(key)
        if cur is None:
            heading_url[key] = {"url": urljoin(page_url, href), "sas": is_sas}
        elif cur["sas"] and not is_sas:
            heading_url[key] = {"url": urljoin(page_url, href), "sas": is_sas}

    # ---- (B) Walk the page text to capture every dated DIY entry -----------
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n")]
    lines = [ln for ln in lines if ln]

    recs, seen = [], set()
    for i, ln in enumerate(lines):
        # A heading line must mention the DIY software AND a benefit year.
        if not keyword_re.search(ln) or "benefit year" not in ln.lower():
            continue
        heading = ln

        # Date: on the same line, else the nearest of the 2 preceding lines.
        dm = DATE_RE.search(heading)
        date_str = dm.group(0) if dm else ""
        if not date_str:
            for j in (i - 1, i - 2):
                if j >= 0:
                    dj = DATE_RE.search(lines[j])
                    if dj and len(lines[j]) <= 40:
                        date_str = dj.group(0)
                        break

        heading_only = DATE_RE.sub("", heading).strip(" -\u2013:\t")
        heading_only = re.sub(r"\s+", " ", heading_only).strip()
        title = f"{date_str} {heading_only}".strip() if date_str else heading_only

        nk = _norm(title)
        if nk in seen:
            continue
        seen.add(nk)

        # Benefit year: prefer the "NNNN Benefit Year" number.
        by = re.search(r"(20\d{2})\s+Benefit\s+Year", title, re.IGNORECASE)
        if by:
            year = by.group(1)
        else:
            ym = re.search(r"20\d{2}", title)
            year = ym.group(0) if ym else ""

        # Attach a zip URL for this entry (keyed by date + heading so entries
        # sharing a heading keep their own URL).
        info = heading_url.get(_norm(title)) or heading_url.get(_norm(heading_only))
        url = info["url"] if info else ""
        ext = ""
        if url:
            ul = url.lower()
            ext = next((e for e in exts if ul.endswith(e)), "")

        recs.append({"year": year, "directory": directory_label,
                     "file_name": title, "file_ext": ext.lstrip("."),
                     "file_url": url})
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
    dated release."""
    source = cfg["alert"].get("source_label", "the watched page")
    lines = [f"{len(new_rows)} new file(s) posted on {source}.", ""]
    for r in sorted(new_rows, key=lambda x: (x["year"], x["file_name"]),
                    reverse=True):
        lines.append(f"- {r['file_name']}")
        detail = f"    Benefit year: {r['year'] or 'n/a'}  |  found {r['date_found']}"
        lines.append(detail)
        if r["file_url"]:
            lines.append(f"    {r['file_url']}")
        lines.append("")  # blank line between entries
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
