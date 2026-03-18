from database import init_db, save_selected_bills, get_active_bills_for_user
import re
import requests
from bs4 import BeautifulSoup
import streamlit as st
from urllib.parse import urljoin, urlparse

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

REQUEST_TIMEOUT = 20


# ----------------------------
# App setup
# ----------------------------
st.set_page_config(page_title="Louisiana Bill Monitor", layout="wide")
st.title("Louisiana Bill Monitor")


# ----------------------------
# Database init
# ----------------------------
@st.cache_resource
def initialize_database():
    init_db()


try:
    initialize_database()
except Exception as e:
    st.error(f"Database initialization failed: {e}")
    st.stop()


# ----------------------------
# Helpers
# ----------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_page_html(url: str) -> str:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def fetch_page(url: str) -> BeautifulSoup:
    html = fetch_page_html(url)
    return BeautifulSoup(html, "html.parser")


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


def load_selected_bills_for_email(email):
    rows = get_active_bills_for_user(email)
    selected = {}

    for row in rows:
        selected[row["bill_number"]] = row["bill_text"]

    return selected


def extract_insurance_committee_links(home_soup):
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
        if label.strip().upper() != "INSURANCE":
            continue
        if "agenda.aspx" not in full_url.lower():
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
        if "SENATE" in context_upper:
            committee_name = "Senate Insurance"
        else:
            committee_name = "House Insurance"

        rows.append(
            {
                "label": "Insurance",
                "committee_name": committee_name,
                "url": full_url,
                "context": row_text,
                "home_date": meeting_date,
                "home_time": time_match.group(1) if time_match else "Time not found",
                "home_room": room_match.group(1).strip() if room_match else "Room not found",
            }
        )

    return rows


def check_insurance_committee_matches(email):
    selected_bills = load_selected_bills_for_email(email)

    if not selected_bills:
        return {
            "matches": [],
            "message": "No saved bills found for this email.",
        }

    home_soup = fetch_page(HOME_URL)
    insurance_links = extract_insurance_committee_links(home_soup)

    if not insurance_links:
        return {
            "matches": [],
            "message": "No Insurance committee meeting links found on the Home page.",
        }

    all_matches = []

    for item in insurance_links:
        try:
            page_soup = fetch_page(item["url"])
            page_bills = extract_bills_from_page(page_soup)

            for bill_number, saved_text in selected_bills.items():
                if bill_number in page_bills:
                    all_matches.append(
                        {
                            "bill_number": bill_number,
                            "saved_text": saved_text,
                            "committee_name": item["committee_name"],
                            "meeting_date": item["home_date"],
                            "meeting_time": item["home_time"],
                            "room": item["home_room"],
                            "meeting_url": item["url"],
                        }
                    )
        except Exception:
            continue

    unique = []
    seen = set()
    for match in all_matches:
        key = (match["bill_number"], match["meeting_url"])
        if key not in seen:
            seen.add(key)
            unique.append(match)

    if unique:
        return {"matches": unique, "message": None}

    return {
        "matches": [],
        "message": "No upcoming bill matched under Insurance committee meetings.",
    }


# ----------------------------
# Session state
# ----------------------------
if "bill_blocks" not in st.session_state:
    st.session_state.bill_blocks = []


# ----------------------------
# Section 1: Load bills
# ----------------------------
st.subheader("1. Load bills from an agenda")

agenda_url = st.text_input(
    "Paste House Order / Senate agenda link",
    placeholder="https://legis.la.gov/legis/agenda.aspx?m=25281",
)

if st.button("Load Bills", use_container_width=True):
    if not agenda_url.strip():
        st.error("Please enter an agenda URL.")
    else:
        try:
            soup = fetch_page(agenda_url.strip())
            bill_blocks = extract_bill_blocks(soup)
            st.session_state.bill_blocks = bill_blocks

            if bill_blocks:
                st.success(f"Loaded {len(bill_blocks)} bills from the agenda.")
            else:
                st.warning("No HB/SB bills were found on this page.")
        except requests.exceptions.RequestException as e:
            st.error(f"Error loading agenda page: {e}")
        except Exception as e:
            st.error(f"Unexpected error loading agenda: {e}")

bill_blocks = st.session_state.bill_blocks


# ----------------------------
# Section 1.2: Select bills
# ----------------------------
if bill_blocks:
    st.subheader("1.2 Select bills to monitor")

    selected_items = []

    for i, block in enumerate(bill_blocks):
        preview_text = block["text"][:180]
        label = f"{block['bill_number']} - {preview_text}..."
        checked = st.checkbox(label, key=f"bill_{i}")
        if checked:
            selected_items.append(block)

    if selected_items:
        st.subheader("1.3 Save selected bills")
        email = st.text_input("Enter your email address")

        if st.button("Save Selected Bills", use_container_width=True):
            if not email.strip():
                st.error("Please enter your email address.")
            else:
                try:
                    save_selected_bills(
                        user_email=email.strip(),
                        agenda_url=agenda_url.strip(),
                        selected_items=selected_items,
                    )

                    st.success(
                        f"Saved {len(selected_items)} selected bill(s) for {email.strip()}."
                    )

                    saved_rows = get_active_bills_for_user(email.strip())
                    if saved_rows:
                        st.write("Currently saved bills:")
                        for row in saved_rows:
                            st.write(f"- {row['bill_number']}")
                except Exception as e:
                    st.error(f"Error saving selected bills: {e}")


# ----------------------------
# Section 2: Check matches
# ----------------------------
st.subheader("2. Check upcoming Insurance committee meetings")

check_email = st.text_input(
    "Enter your saved email address to check for Insurance committee matches"
)

if st.button("Check Insurance Committee Matches", use_container_width=True):
    if not check_email.strip():
        st.error("Please enter your email address.")
    else:
        try:
            result = check_insurance_committee_matches(check_email.strip())

            if result["matches"]:
                st.success("Matched bill(s) found in upcoming Insurance committee meetings.")

                for match in result["matches"]:
                    st.markdown(f"### {match['bill_number']}")
                    st.write(f"**Committee:** {match['committee_name']}")
                    st.write(f"**Date:** {match['meeting_date']}")
                    st.write(f"**Time:** {match['meeting_time']}")
                    st.write(f"**Room:** {match['room']}")
                    st.write(f"**Saved description:** {match['saved_text']}")
                    st.link_button("Open meeting link", match["meeting_url"])
                    st.divider()
            else:
                st.info(result["message"])
        except requests.exceptions.RequestException as e:
            st.error(f"Error checking committee pages: {e}")
        except Exception as e:
            st.error(f"Unexpected error checking matches: {e}")