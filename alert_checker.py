import os
import re
import requests
import smtplib
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from supabase import create_client, Client

HOME_URL = "https://legis.la.gov/Legis/Home.aspx"

BILL_START_PATTERN = re.compile(r"^(HB|SB)\s*\d+\b", re.IGNORECASE)
BILL_NUMBER_PATTERN = re.compile(r"\b(HB|SB)\s*\d+\b", re.IGNORECASE)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def fetch_page(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def clean_bill_number(text):
    match = re.search(r"\b(HB|SB)\s*(\d+)\b", str(text).strip(), re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}{match.group(2)}"
    return str(text).strip().upper().replace(" ", "")


def is_valid_web_link(url):
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https")


def deduplicate_bill_blocks(bill_blocks):
    seen = set()
    unique_blocks = []

    for block in bill_blocks:
        bill_number = block["bill_number"]
        if bill_number not in seen:
            seen.add(bill_number)
            unique_blocks.append(block)

    return unique_blocks


def extract_bill_blocks(soup):
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    bill_blocks = []
    current_bill = None
    current_lines = []

    for line in lines:
        if BILL_START_PATTERN.match(line):
            if current_bill is not None:
                bill_blocks.append(
                    {
                        "bill_number": current_bill,
                        "text": " ".join(current_lines),
                    }
                )

            current_bill = clean_bill_number(line)
            current_lines = [line]
        elif current_bill is not None:
            current_lines.append(line)

    if current_bill is not None:
        bill_blocks.append(
            {
                "bill_number": current_bill,
                "text": " ".join(current_lines),
            }
        )

    return deduplicate_bill_blocks(bill_blocks)


def extract_bills_from_page(soup):
    bill_blocks = extract_bill_blocks(soup)

    if bill_blocks:
        return {block["bill_number"]: block["text"] for block in bill_blocks}

    page_text = soup.get_text(" ", strip=True)
    results = {}
    for match in re.finditer(BILL_NUMBER_PATTERN, page_text):
        bill_number = clean_bill_number(match.group(0))
        results[bill_number] = bill_number

    return results


def extract_committee_links(home_soup):
    rows = []
    seen = set()

    home_text = home_soup.get_text("\n", strip=True)
    date_match = re.search(
        r"TODAY'S MEETINGS,\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})",
        home_text,
        re.IGNORECASE,
    )
    meeting_date = date_match.group(1) if date_match else "Date not found"

    for a in home_soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = a["href"].strip()
        full_url = urljoin(HOME_URL, href)

        if not is_valid_web_link(full_url):
            continue
        if "agenda.aspx" not in full_url.lower():
            continue
        if not label:
            continue

        tr = a.find_parent("tr")
        if tr is None:
            row_text = a.parent.get_text(" ", strip=True)
        else:
            row_text = tr.get_text(" ", strip=True)

        key = (full_url, row_text)
        if key in seen:
            continue
        seen.add(key)

        time_match = re.search(
            r"(\d{1,2}:\d{2}\s*(a\.m\.|p\.m\.|A\.M\.|P\.M\.|am|pm))",
            row_text,
            re.IGNORECASE,
        )

        room_match = re.search(
            r"(Room\s*[A-Z0-9\-]+)",
            row_text,
            re.IGNORECASE,
        )

        context_upper = row_text.upper()
        chamber = "Senate" if "SENATE" in context_upper else "House"
        committee_name = f"{chamber} {label.strip()}"

        rows.append(
            {
                "committee_name": committee_name,
                "url": full_url,
                "meeting_date": meeting_date,
                "meeting_time": time_match.group(1) if time_match else "Time not found",
                "room": room_match.group(1).strip() if room_match else "Room not found",
            }
        )

    return rows


def get_all_active_tracked_bills_grouped(supabase: Client):
    response = (
        supabase.table("tracked_bills")
        .select("user_email,bill_number,bill_text,agenda_url")
        .eq("is_active", True)
        .execute()
    )

    rows = response.data or []

    grouped = {}
    for row in rows:
        email = row["user_email"].strip().lower()
        bill_number = row["bill_number"].strip().upper()
        bill_text = row.get("bill_text", "")

        if email not in grouped:
            grouped[email] = {}

        grouped[email][bill_number] = bill_text

    return grouped


def log_sent_alert(supabase: Client, user_email: str, bill_number: str, meeting_url: str):
    supabase.table("sent_alerts").insert(
        {
            "user_email": user_email.lower(),
            "bill_number": bill_number.upper(),
            "meeting_url": meeting_url,
            "sent_date": date.today().isoformat(),
        }
    ).execute()


def find_matches(supabase: Client):
    users = get_all_active_tracked_bills_grouped(supabase)
    if not users:
        return []

    home_soup = fetch_page(HOME_URL)
    committee_links = extract_committee_links(home_soup)

    results = []

    for email, selected_bills in users.items():
        user_matches = []

        for item in committee_links:
            try:
                page_soup = fetch_page(item["url"])
                page_bills = extract_bills_from_page(page_soup)

                for bill_number, saved_text in selected_bills.items():
                    if bill_number in page_bills:
                        user_matches.append(
                            {
                                "email": email,
                                "bill_number": bill_number,
                                "saved_text": saved_text,
                                "committee_name": item["committee_name"],
                                "meeting_date": item["meeting_date"],
                                "meeting_time": item["meeting_time"],
                                "room": item["room"],
                                "meeting_url": item["url"],
                            }
                        )
            except Exception as e:
                print(f"Error processing {item['url']}: {e}")
                continue

        seen = set()
        deduped = []
        for m in user_matches:
            key = (m["bill_number"], m["meeting_url"])
            if key not in seen:
                seen.add(key)
                deduped.append(m)

        results.extend(deduped)

    return results


def send_email(to_email, subject, body):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ["FROM_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())


def build_email_body(items):
    lines = []

    for item in items:
        lines.append(
            f"""
            <p>
            <b>Bill:</b> {item['bill_number']}<br>
            <b>Committee:</b> {item['committee_name']}<br>
            <b>Date:</b> {item['meeting_date']}<br>
            <b>Time:</b> {item['meeting_time']}<br>
            <b>Room:</b> {item['room']}<br>
            <b>Description:</b> {item['saved_text']}<br>
            <b>Meeting Link:</b> <a href="{item['meeting_url']}">Open Agenda</a>
            </p>
            <hr>
            """
        )

    return f"""
    <h2>Louisiana Bill Alert</h2>
    <p>The following saved bill(s) matched upcoming committee meetings:</p>
    {''.join(lines)}
    """


def main():
    supabase = get_supabase()
    matches = find_matches(supabase)

    if not matches:
        print("No upcoming bill matched.")
        return

    grouped = {}
    for match in matches:
        grouped.setdefault(match["email"], []).append(match)

    for email, items in grouped.items():
        if not items:
            continue

        subject = f"Bill Alert: {len(items)} bill(s) matched"
        body = build_email_body(items)

        send_email(email, subject, body)

        for item in items:
            log_sent_alert(
                supabase,
                email,
                item["bill_number"],
                item["meeting_url"],
            )

        print(f"Email sent to {email}")


if __name__ == "__main__":
    main()
