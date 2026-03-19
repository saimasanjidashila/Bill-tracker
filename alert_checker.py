import os
import re
import smtplib
import ssl
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin, urlparse

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


def already_sent_alert(supabase: Client, user_email: str, bill_number: str, agenda_url: str | None = None) -> bool:
    bill_number = normalize_bill_number(bill_number)

    try:
        query = (
            supabase.table("sent_alerts")
            .select("*")
            .eq("user_email", user_email)
            .eq("bill_number", bill_number)
        )

        if agenda_url:
            query = query.eq("agenda_url", agenda_url)

        response = query.execute()
        exists = bool(response.data)
        log(
            f"Duplicate check => user_email={user_email}, bill_number={bill_number}, "
            f"agenda_url={agenda_url}, exists={exists}"
        )
        return exists
    except Exception as e:
        log(f"ERROR checking sent_alerts duplicate: {e}")
        return False


def insert_sent_alert(
    supabase: Client,
    user_email: str,
    bill_number: str,
    agenda_url: str,
    meeting_title: str = "",
    meeting_date: str = "",
):
    bill_number = normalize_bill_number(bill_number)

    rich_payload = {
        "user_email": user_email,
        "bill_number": bill_number,
        "agenda_url": agenda_url,
        "meeting_title": meeting_title,
        "meeting_date": meeting_date,
    }

    minimal_payload = {
        "user_email": user_email,
        "bill_number": bill_number,
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
# SCRAPING
# =========================

def discover_agenda_links(session: requests.Session) -> list[dict]:
    """
    Finds agenda links from the home page.
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
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)

        absolute = urljoin(HOME_URL, href)
        href_lower = href.lower()
        text_lower = text.lower()

        if (
            "agenda" in href_lower
            or "agenda" in text_lower
            or "meeting" in text_lower
            or "committee" in text_lower
        ):
            if absolute not in seen:
                seen.add(absolute)
                results.append(
                    {
                        "meeting_title": text or "Agenda Link",
                        "agenda_url": absolute,
                        "source": "home_page",
                    }
                )

    log(f"Discovered {len(results)} possible agenda/meeting links from home page.")

    print("\n=== DISCOVERED AGENDA LINKS ===")
    for item in results:
        print(item)
    print("=== END DISCOVERED AGENDA LINKS ===\n")

    return results


def fetch_agenda_details(session: requests.Session, agenda_item: dict) -> dict | None:
    agenda_url = agenda_item["agenda_url"]
    response = safe_get(session, agenda_url)
    if not response:
        return None

    text = clean_text(response.text)
    found_bills = extract_bill_numbers(text)

    details = {
        "meeting_title": agenda_item.get("meeting_title", ""),
        "agenda_url": agenda_url,
        "source": agenda_item.get("source", ""),
        "page_text": text,
        "bills": found_bills,
        "meeting_date": str(date.today()),
    }

    print("\n=== AGENDA DETAILS ===")
    print("Meeting title:", details["meeting_title"])
    print("Agenda URL:", details["agenda_url"])
    print("Bills found:", details["bills"])
    print("=== END AGENDA DETAILS ===\n")

    return details


def load_all_agendas(session: requests.Session) -> list[dict]:
    discovered = discover_agenda_links(session)
    agendas = []

    log("Loading details for each discovered agenda link...")

    for item in discovered:
        details = fetch_agenda_details(session, item)
        if details:
            agendas.append(details)

    log(f"Loaded {len(agendas)} agenda pages successfully.")

    print("\n=== ALL MEETINGS FOUND ===")
    for agenda in agendas:
        print(
            {
                "meeting_title": agenda.get("meeting_title"),
                "agenda_url": agenda.get("agenda_url"),
                "meeting_date": agenda.get("meeting_date"),
                "bills": agenda.get("bills"),
            }
        )
    print("=== END ALL MEETINGS FOUND ===\n")

    return agendas


# =========================
# EMAIL
# =========================

def send_email(to_email: str, subject: str, html_body: str):
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_FROM:
        raise ValueError("SMTP configuration is missing. Check SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM.")

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


def build_email_html(bill_number: str, meeting_title: str, meeting_date: str, agenda_url: str) -> str:
    return f"""
    <html>
      <body>
        <p>Hello,</p>
        <p>Your tracked bill <strong>{bill_number}</strong> was found in a legislative agenda.</p>

        <p>
          <strong>Meeting:</strong> {meeting_title or "Committee Meeting"}<br>
          <strong>Date Checked:</strong> {meeting_date}<br>
          <strong>Agenda URL:</strong> <a href="{agenda_url}">{agenda_url}</a>
        </p>

        <p>Please review the agenda for details.</p>

        <p>Regards,<br>Bill Tracker</p>
      </body>
    </html>
    """


# =========================
# MATCHING / PROCESSING
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

        agendas = load_all_agendas(session)
        if not agendas:
            log("No agenda pages found. Exiting.")
            return

        tracked_by_bill = {}
        for row in tracked_rows:
            normalized = normalize_bill_number(row.get("bill_number", ""))
            if not normalized:
                continue
            tracked_by_bill.setdefault(normalized, []).append(row)

        print("\n=== TRACKED BILL MAP ===")
        for bill, rows in tracked_by_bill.items():
            print(bill, "=>", [{"id": r.get("id"), "user_email": r.get("user_email")} for r in rows])
        print("=== END TRACKED BILL MAP ===\n")

        total_matches = 0
        total_sent = 0

        for agenda in agendas:
            agenda_url = agenda.get("agenda_url", "")
            meeting_title = agenda.get("meeting_title", "")
            meeting_date = agenda.get("meeting_date", str(date.today()))
            agenda_bills = [normalize_bill_number(b) for b in agenda.get("bills", [])]
            agenda_bill_set = set(agenda_bills)

            print("\n=== MATCHING FOR AGENDA ===")
            print("Meeting:", meeting_title)
            print("Agenda URL:", agenda_url)
            print("Agenda normalized bills:", sorted(agenda_bill_set))
            print("Tracked normalized bills:", sorted(tracked_by_bill.keys()))

            matches = sorted(agenda_bill_set.intersection(set(tracked_by_bill.keys())))
            print("Matches:", matches)
            print("=== END MATCHING FOR AGENDA ===\n")

            if not matches:
                continue

            for matched_bill in matches:
                total_matches += 1
                matching_rows = tracked_by_bill.get(matched_bill, [])

                for row in matching_rows:
                    user_email = row.get("user_email")
                    if not user_email:
                        log(f"Skipping {matched_bill}: missing user_email in tracked row {row}")
                        continue

                    if already_sent_alert(supabase, user_email, matched_bill, agenda_url):
                        log(
                            f"Skipping email because alert already exists => "
                            f"user={user_email}, bill={matched_bill}, agenda_url={agenda_url}"
                        )
                        continue

                    subject = f"Bill Alert: {matched_bill} found on agenda"
                    html_body = build_email_html(
                        bill_number=matched_bill,
                        meeting_title=meeting_title,
                        meeting_date=meeting_date,
                        agenda_url=agenda_url,
                    )

                    try:
                        print("\n=== ABOUT TO SEND EMAIL ===")
                        print("To:", user_email)
                        print("Bill:", matched_bill)
                        print("Meeting:", meeting_title)
                        print("Agenda URL:", agenda_url)
                        print("=== END ABOUT TO SEND EMAIL ===\n")

                        send_email(user_email, subject, html_body)

                        print("\n=== EMAIL SENT OK ===")
                        print("To:", user_email)
                        print("Bill:", matched_bill)
                        print("=== END EMAIL SENT OK ===\n")

                        insert_sent_alert(
                            supabase=supabase,
                            user_email=user_email,
                            bill_number=matched_bill,
                            agenda_url=agenda_url,
                            meeting_title=meeting_title,
                            meeting_date=meeting_date,
                        )

                        total_sent += 1

                    except Exception as e:
                        log(
                            f"ERROR sending alert => user={user_email}, bill={matched_bill}, "
                            f"agenda_url={agenda_url}, error={e}"
                        )

        log(f"Total matched bill occurrences: {total_matches}")
        log(f"Total emails sent: {total_sent}")
        log("==== END alert-checker.py ====")

    except Exception as e:
        log(f"FATAL ERROR in process_alerts(): {e}")


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    process_alerts()
