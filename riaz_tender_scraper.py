#!/usr/bin/env python3

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "tender_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

KEYWORDS = [
    "tender",
    "rfp",
    "proposal",
    "quotation",
    "bid",
    "procurement",
    "e-procurement",
    "request for proposal",
    "notice",
]


def load_state():
    if not STATE_FILE.exists():
        return {"seen": {}, "last_run": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid state format")
        data.setdefault("seen", {})
        data.setdefault("last_run", None)
        return data
    except Exception:
        return {"seen": {}, "last_run": None}


def save_state(data):
    STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def dedupe_key(title, url):
    text = f"{title} {url}".lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def extract_date_candidates(text):
    patterns = [
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    ]
    matches = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return matches[:5]


def looks_like_tender_link(text, href):
    combined = f"{text} {href}".lower()
    return any(keyword in combined for keyword in KEYWORDS)


def get_candidate_urls():
    configured = os.getenv("TENDER_URLS", "").strip()
    if configured:
        urls = []
        for item in re.split(r"[\n,]+", configured):
            value = item.strip()
            if value:
                urls.append(value)
        return urls

    return [
        "https://www.ppra.org.pk/",
        "https://www.punjab.gov.pk/",
        "https://www.pacra.com.pk/",
        "https://www.pesrp.edu.pk/",
    ]


def fetch_html(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
        return response.text
    except Exception as exc:
        print(f"[WARN] Failed to fetch {url}: {exc}")
        return ""


def parse_tenders_from_html(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    tenders = []
    seen_links = set()

    for a in soup.find_all("a", href=True):
        text = normalize_text(a.get_text(" ", strip=True))
        href = a.get("href", "").strip()
        if not href or href.startswith("javascript:"):
            continue
        if not looks_like_tender_link(text, href):
            continue
        url = urljoin(source_url, href)
        if url in seen_links:
            continue
        seen_links.add(url)

        title = text or urlparse(url).path.strip("/")
        title = normalize_text(title)
        if len(title) < 8:
            continue

        tenders.append({
            "title": title,
            "url": url,
            "source": source_url,
            "date": "",
        })

    for item in tenders:
        try:
            page_html = fetch_html(item["url"])
            if not page_html:
                continue
            page_soup = BeautifulSoup(page_html, "html.parser")
            page_text = normalize_text(page_soup.get_text(" ", strip=True))
            dates = extract_date_candidates(page_text)
            if dates:
                item["date"] = dates[0]
        except Exception:
            pass

    return tenders


def send_email(new_tenders):
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    to_email = os.getenv("EMAIL_TO")
    from_email = os.getenv("EMAIL_FROM")

    if not all([host, username, password, to_email, from_email]):
        print("[INFO] Email settings are not configured. Skipping notification email.")
        return

    port = int(os.getenv("SMTP_PORT", "587"))

    subject = f"Tender Watch: {len(new_tenders)} new tender(s) found"
    body_lines = [
        "New tender notices were found by the daily scraper.",
        "",
    ]

    for tender in new_tenders[:10]:
        line = f"- {tender['title']}"
        if tender.get("date"):
            line += f" ({tender['date']})"
        line += f"\n  {tender['url']}"
        body_lines.append(line)

    if len(new_tenders) > 10:
        body_lines.append(f"\n... and {len(new_tenders) - 10} more.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content("\n".join(body_lines))

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
        print(f"[INFO] Email notification sent for {len(new_tenders)} new tender(s).")
    except Exception as exc:
        print(f"[WARN] Email notification failed: {exc}")


def main():
    state = load_state()
    seen = state.get("seen", {})
    all_found = []

    for source_url in get_candidate_urls():
        html = fetch_html(source_url)
        if not html:
            continue
        tenders = parse_tenders_from_html(html, source_url)
        for tender in tenders:
            key = dedupe_key(tender["title"], tender["url"])
            if key in seen:
                continue
            all_found.append(tender)
            seen[key] = {
                "title": tender["title"],
                "url": tender["url"],
                "source": tender["source"],
                "date": tender.get("date", ""),
                "added_at": datetime.now(timezone.utc).isoformat(),
            }

    state["seen"] = seen
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    if not all_found:
        print("[INFO] No new tender notices found.")
        return 0

    print(f"[INFO] Found {len(all_found)} new tender notice(s).")
    send_email(all_found)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] Scraper crashed: {exc}", file=sys.stderr)
        raise SystemExit(1)
