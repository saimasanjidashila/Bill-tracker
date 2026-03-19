
import os
import re
import smtplib
import ssl
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client


# =========================
# CONFIG
# =========================

HOME_URL = "https://legis.la.gov/Legis/Home.aspx"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

BILL_PATTERN = re.compile(r"\b(HB|SB)\s*-?\s*(\d+)\b", re.IGNORECASE)

MONTH_DATE_YEAR_PATTERN = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|September|October|November|December"
    r")\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)

SHORT_DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")

TIME_PATTERN = re.compile(
    r"\b\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?|AM|PM)\b",
    re.IGNORECASE,
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USERNAME)

REQUEST_TIMEOUT = 30


# =========================
# LOGGING
# =========================

def log(message: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


# =========================
# HELPERS
# =========================

def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_bill_number(bill: str) -> str:
    if not bill:
        return ""
    bill = bill.strip().upper()
    bill = re.sub(r"\s+", "", bill)
    bill = bill.replace("-", "")
    return bill


def extract_bill_numbers(text: str) -> list[str]:
    found = set()
    if not text:
        return []

    for match in BILL_PATTERN.finditer(text):
        chamber = match.group(1).upper()
        number = match.group(2)
        found.add(f"{chamber}{number}")

    return sorted(found)


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def get_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def safe_get(session: requests.Session, url: str) -> requests.Response | None:
    try:
        log(f"GET {url}")
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        log(f"SUCCESS {url} [{response.status_code}]")
        return response
    except Exception as e:
        log(f"ERROR fetching {url}: {e}")
        return None


def parse_date_string(text: str) -> str:
    text = normalize_whitespace(text)

    match = MONTH_DATE_YEAR_PATTERN.search(text)
    if match:
        return match.group(0)

    match = SHORT_DATE_PATTERN.search(text)
    if match:
        return match.group(0)

    return ""


def parse_time_string(text: str) -> str:
    text = normalize_whitespace(text)
    match = TIME_PATTERN.search(text)
    if match:
        return normalize_whitespace(match.group(0))
    return ""


def format_date_for_email(meeting_date: str) -> str:
    if not meeting_date:
        return "Not found"

    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(meeting_date, fmt)
            return f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            continue

    return meeting_date


def meeting_date_to_iso(meeting_date: str) -> str:
    if not meeting_date:
        return ""

    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(meeting_date, fmt).date()
            return dt.isoformat()
        except ValueError:
            continue

    return ""


def is_today_date(meeting_date: str) -> bool:
    iso = meeting_date_to_iso(meeting_date)
    return iso == date.today().isoformat()


def parse_room(text: str) -> str:
    text = normalize_whitespace(text)

    patterns = [
        re.compile(r"\bHouse Chamber\b", re.IGNORECASE),
        re.compile(r"\bSenate Chamber\b", re.IGNORECASE),
        re.compile(r"\bCommittee Room\s+[A-Za-z0-9\-]+\b", re.IGNORECASE),
        re.compile(r"\bRoom\s+[A-Za-z0-9\-]+\b", re.IGNORECASE),
    ]

    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = normalize_whitespace(match.group(0))
            # avoid dragging in extra words
            value = re.split(r"\b(Adjourned|Scheduled|Cancelled|Canceled|Click here)\b", value, flags=re.IGNORECASE)[0].strip()
            return value

    return ""


def build_source_key(user_email: str, bill_number: str, meeting_date: str, source_url: str) -> str:
    return f"{user_email}|||{bill_number}|||{meeting_date}|||{source_url}"


# =========================
# SUPABASE
# =========================

def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL or SUPABASE_KEY is missing in environment variables.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_active_tracked_bills(supabase: Client) -> list[dict]:
    log("Loading active tracked bills from Supabase...")
    response = (
        supabase.table("tracked_bills")
        .select("*")
        .eq("is_active", True)
        .execute()
    )
    rows = response.data or []

    log(f"Active tracked bills count: {len(rows)}")
    print("\n=== ACTIVE TRACKED BILLS ===")
    for row in rows:
        print(
            {
                "id": row.get("id"),
                "user_email": row.get("user_email"),
                "bill_number": row.get("bill_number"),
                "bill_text": row.get("bill_text"),
                "agenda_url": row.get("agenda_url"),
                "is_active": row.get("is_active"),
            }
        )
    print("=== END ACTIVE TRACKED BILLS ===\n")

    return rows


def already_sent_alert_today(
    supabase: Client,
    user_email: str,
    bill_number: str,
    alert_date_iso: str,
) -> bool:
    bill_number = normalize_bill_number(bill_number)

    try:
        response = (
            supabase.table("sent_alerts")
            .select("*")
            .eq("user_email", user_email)
            .eq("bill_number", bill_number)
            .eq("meeting_date", alert_date_iso)
            .execute()
        )
        exists = bool(response.data)
        log(
            f"Duplicate check => user_email={user_email}, "
            f"bill_number={bill_number}, meeting_date={alert_date_iso}, exists={exists}"
        )
        return exists
    except Exception as e:
        log(f"ERROR checking sent_alerts duplicate: {e}")
        return False


def insert_sent_alert(
    supabase: Client,
    user_email: str,
    bill_number: str,
    source_url: str,
    meeting_title: str = "",
    meeting_date_iso: str = "",
    meeting_time: str = "",
    meeting_room: str = "",
    source_type: str = "",
):
    bill_number = normalize_bill_number(bill_number)

    rich_payload = {
        "user_email": user_email,
        "bill_number": bill_number,
        "agenda_url": source_url,
        "meeting_title": meeting_title,
        "meeting_date": meeting_date_iso,
        "meeting_time": meeting_time,
        "meeting_room": meeting_room,
        "source_type": source_type,
    }

    minimal_payload = {
        "user_email": user_email,
        "bill_number": bill_number,
        "meeting_date": meeting_date_iso,
    }

    try:
        log(f"Inserting into sent_alerts (rich payload): {rich_payload}")
        supabase.table("sent_alerts").insert(rich_payload).execute()
        log("Inserted sent_alerts row successfully with rich payload.")
        return
    except Exception as e:
        log(f"Rich insert failed: {e}")

    try:
        log(f"Trying minimal insert into sent_alerts: {minimal_payload}")
        supabase.table("sent_alerts").insert(minimal_payload).execute()
        log("Inserted sent_alerts row successfully with minimal payload.")
    except Exception as e:
        log(f"Minimal insert also failed: {e}")


# =========================
# SCRAPING SOURCES
# =========================

def discover_home_special_links(session: requests.Session) -> list[dict]:
    """
    From home page, find Order of the Day and Daily Digest links.
    """
    results = []
    seen = set()

    response = safe_get(session, HOME_URL)
    if not response:
        return results

    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)

    log(f"Found {len(links)} links on home page.")

    for a in links:
        href = (a.get("href") or "").strip()
        text = normalize_whitespace(a.get_text(" ", strip=True))
        if not href:
            continue

        text_lower = text.lower()
        absolute = urljoin(HOME_URL, href)

        if "order of the day" in text_lower or "daily digest" in text_lower:
            if absolute not in seen:
                seen.add(absolute)
                results.append(
                    {
                        "source_type": "order_of_the_day" if "order of the day" in text_lower else "daily_digest",
                        "title": text,
                        "url": absolute,
                    }
                )

    print("\n=== HOME SPECIAL LINKS ===")
    for item in results:
        print(item)
    print("=== END HOME SPECIAL LINKS ===\n")

    return results


def discover_today_agenda_links_from_home(session: requests.Session) -> list[dict]:
    """
    Keep current real meeting agenda pages from home page too.
    """
    results = []
    seen = set()

    response = safe_get(session, HOME_URL)
    if not response:
        return results

    soup = BeautifulSoup(response.text, "html.parser")
    links = soup.find_all("a", href=True)

    for a in links:
        href = (a.get("href") or "").strip()
        text = normalize_whitespace(a.get_text(" ", strip=True))
        absolute = urljoin(HOME_URL, href)

        if "agenda.aspx?m=" in href.lower():
            if absolute not in seen:
                seen.add(absolute)
                results.append(
                    {
                        "source_type": "meeting_agenda",
                        "title": text or "Agenda",
                        "url": absolute,
                    }
                )

    print("\n=== HOME DIRECT AGENDA LINKS ===")
    for item in results:
        print(item)
    print("=== END HOME DIRECT AGENDA LINKS ===\n")

    return results


def fetch_source_details(session: requests.Session, source_item: dict) -> dict | None:
    response = safe_get(session, source_item["url"])
    if not response:
        return None

    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(html)

    meeting_date = parse_date_string(text)
    meeting_time = parse_time_string(text)
    meeting_room = parse_room(text)
    bills = extract_bill_numbers(text)

    title = source_item.get("title", "")
    if not title:
        if soup.title:
            title = normalize_whitespace(soup.title.get_text(" ", strip=True))

    details = {
        "source_type": source_item.get("source_type", ""),
        "meeting_title": title or "Legislative Meeting",
        "source_url": source_item["url"],
        "meeting_date": meeting_date,
        "meeting_time": meeting_time,
        "meeting_room": meeting_room,
        "bills": bills,
        "page_text": text,
    }

    print("\n=== SOURCE DETAILS ===")
    print("Source type:", details["source_type"])
    print("Title:", details["meeting_title"])
    print("URL:", details["source_url"])
    print("Meeting date:", details["meeting_date"])
    print("Meeting time:", details["meeting_time"])
    print("Meeting room:", details["meeting_room"])
    print("Bills found:", details["bills"])
    print("=== END SOURCE DETAILS ===\n")

    return details


def load_sources_for_today(session: requests.Session) -> list[dict]:
    all_sources = []

    special_links = discover_home_special_links(session)
    direct_agendas = discover_today_agenda_links_from_home(session)

    combined = special_links + direct_agendas
    if not combined:
        return []

    for item in combined:
        details = fetch_source_details(session, item)
        if not details:
            continue

        if not details.get("meeting_date"):
            log(f"Skipping source without date: {details.get('source_url')}")
            continue

        if not is_today_date(details["meeting_date"]):
            log(
                f"Skipping non-today source => {details['source_url']} "
                f"with date {details['meeting_date']}"
            )
            continue

        all_sources.append(details)

    print("\n=== TODAY SOURCES ONLY ===")
    for item in all_sources:
        print(
            {
                "source_type": item["source_type"],
                "meeting_title": item["meeting_title"],
                "source_url": item["source_url"],
                "meeting_date": item["meeting_date"],
                "meeting_time": item["meeting_time"],
                "meeting_room": item["meeting_room"],
                "bills": item["bills"],
            }
        )
    print("=== END TODAY SOURCES ONLY ===\n")

    return all_sources


# =========================
# EMAIL
# =========================

def send_email(to_email: str, subject: str, html_body: str):
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_FROM:
        raise ValueError(
            "SMTP configuration is missing. Check SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM."
        )

    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()

    log(f"Connecting to SMTP server {SMTP_HOST}:{SMTP_PORT}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        log(f"Sending email to {to_email}...")
        server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        log(f"Email sent successfully to {to_email}.")


def build_combined_email_html(alerts: list[dict]) -> str:
    if not alerts:
        return ""

    first_title = alerts[0].get("meeting_title", "meeting")
    intro = (
        "<p style='font-size:28px; font-weight:700; margin-bottom:20px;'>"
        "📢 Louisiana Bill Alert"
        "</p>"
        f"<p>The following saved bill(s) are scheduled for discussion in today's "
        f"{first_title}:</p>"
    )

    sections = []
    for alert in alerts:
        bill_number = alert.get("bill_number", "")
        committee = alert.get("meeting_title", "Not found")
        meeting_date = format_date_for_email(alert.get("meeting_date", "")) or "Not found"
        meeting_time = alert.get("meeting_time") or "Not found"
        meeting_room = alert.get("meeting_room") or "Not found"
        bill_text = alert.get("bill_text") or "Not found"
        source_url = alert.get("source_url", "")

        section = f"""
        <div style="margin-bottom:22px; padding-bottom:14px; border-bottom:1px solid #999;">
          <div><strong>Bill:</strong> {bill_number}</div>
          <div><strong>Committee:</strong> {committee}</div>
          <div><strong>Date:</strong> {meeting_date}</div>
          <div><strong>Time:</strong> {meeting_time}</div>
          <div><strong>Room:</strong> {meeting_room}</div>
          <div><strong>Description:</strong> {bill_text}</div>
          <div><strong>Meeting Link:</strong> <a href="{source_url}">Open Agenda</a></div>
        </div>
        """
        sections.append(section)

    closing = "<p>Regards,<br>Bill Tracker</p>"

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; font-size:16px; line-height:1.45;">
        {intro}
        {''.join(sections)}
        {closing}
      </body>
    </html>
    """


# =========================
# MAIN PROCESS
# =========================

def process_alerts():
    log("==== START alert-checker.py ====")

    try:
        supabase = get_supabase()
        session = get_requests_session()

        tracked_rows = get_active_tracked_bills(supabase)
        if not tracked_rows:
            log("No active tracked bills found. Exiting.")
            return

        today_sources = load_sources_for_today(session)
        if not today_sources:
            log("No matching today sources found. Exiting.")
            return

        tracked_by_bill = {}
        for row in tracked_rows:
            normalized = normalize_bill_number(row.get("bill_number", ""))
            if normalized:
                tracked_by_bill.setdefault(normalized, []).append(row)

        print("\n=== TRACKED BILL MAP ===")
        for bill, rows in tracked_by_bill.items():
            print(bill, "=>", [{"id": r.get("id"), "user_email": r.get("user_email")} for r in rows])
        print("=== END TRACKED BILL MAP ===\n")

        grouped_alerts: dict[str, dict] = {}
        total_new_matches = 0

        for source in today_sources:
            source_url = source["source_url"]
            meeting_title = source["meeting_title"]
            meeting_date = source["meeting_date"]
            meeting_time = source["meeting_time"]
            meeting_room = source["meeting_room"]
            source_type = source["source_type"]

            source_bills = {normalize_bill_number(b) for b in source.get("bills", [])}
            matches = sorted(source_bills.intersection(set(tracked_by_bill.keys())))

            print("\n=== MATCHING FOR SOURCE ===")
            print("Source type:", source_type)
            print("Title:", meeting_title)
            print("URL:", source_url)
            print("Date:", meeting_date)
            print("Time:", meeting_time)
            print("Room:", meeting_room)
            print("Source bills:", sorted(source_bills))
            print("Tracked bills:", sorted(tracked_by_bill.keys()))
            print("Matches:", matches)
            print("=== END MATCHING FOR SOURCE ===\n")

            if not matches:
                continue

            meeting_date_iso = meeting_date_to_iso(meeting_date)

            for matched_bill in matches:
                for row in tracked_by_bill.get(matched_bill, []):
                    user_email = row.get("user_email")
                    if not user_email:
                        continue

                    if already_sent_alert_today(supabase, user_email, matched_bill, meeting_date_iso):
                        log(
                            f"Skipping already-sent-today bill => "
                            f"user={user_email}, bill={matched_bill}, date={meeting_date_iso}"
                        )
                        continue

                    total_new_matches += 1
                    group_key = f"{user_email}|||{meeting_date_iso}"

                    if group_key not in grouped_alerts:
                        grouped_alerts[group_key] = {
                            "user_email": user_email,
                            "meeting_date_iso": meeting_date_iso,
                            "alerts": [],
                        }

                    grouped_alerts[group_key]["alerts"].append({
                        "bill_number": matched_bill,
                        "bill_text": row.get("bill_text", ""),
                        "meeting_title": meeting_title,
                        "meeting_date": meeting_date,
                        "meeting_time": meeting_time,
                        "meeting_room": meeting_room,
                        "source_url": source_url,
                        "source_type": source_type,
                    })

        print("\n=== GROUPED ALERTS TO SEND ===")
        for key, payload in grouped_alerts.items():
            print(
                {
                    "user_email": payload["user_email"],
                    "meeting_date_iso": payload["meeting_date_iso"],
                    "bill_numbers": [a["bill_number"] for a in payload["alerts"]],
                }
            )
        print("=== END GROUPED ALERTS TO SEND ===\n")

        total_emails_sent = 0

        for _, payload in grouped_alerts.items():
            user_email = payload["user_email"]
            alerts = payload["alerts"]

            if not alerts:
                continue

            # de-duplicate repeated bill/source combos inside same email
            seen = set()
            deduped_alerts = []
            for a in alerts:
                key = (a["bill_number"], a["meeting_date"], a["source_url"])
                if key not in seen:
                    seen.add(key)
                    deduped_alerts.append(a)

            subject = "Louisiana Bill Alert"
            html_body = build_combined_email_html(deduped_alerts)

            try:
                print("\n=== ABOUT TO SEND EMAIL ===")
                print("To:", user_email)
                print("Bills:", [a["bill_number"] for a in deduped_alerts])
                print("=== END ABOUT TO SEND EMAIL ===\n")

                send_email(user_email, subject, html_body)
                total_emails_sent += 1

                for a in deduped_alerts:
                    insert_sent_alert(
                        supabase=supabase,
                        user_email=user_email,
                        bill_number=a["bill_number"],
                        source_url=a["source_url"],
                        meeting_title=a["meeting_title"],
                        meeting_date_iso=meeting_date_to_iso(a["meeting_date"]),
                        meeting_time=a["meeting_time"],
                        meeting_room=a["meeting_room"],
                        source_type=a["source_type"],
                    )

            except Exception as e:
                log(f"ERROR sending combined email => user={user_email}, error={e}")

        log(f"Total unsent matched bill occurrences grouped: {total_new_matches}")
        log(f"Total combined emails sent: {total_emails_sent}")
        log("==== END alert-checker.py ====")

    except Exception as e:
        log(f"FATAL ERROR in process_alerts(): {e}")


if __name__ == "__main__":
    process_alerts()
