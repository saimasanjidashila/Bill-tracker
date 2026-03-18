import streamlit as st
import psycopg
from psycopg.rows import dict_row


def get_database_url():
    # Streamlit Cloud / local .streamlit/secrets.toml
    if "DATABASE_URL" in st.secrets:
        return st.secrets["DATABASE_URL"]

    raise ValueError("DATABASE_URL is not set in Streamlit secrets.")


def get_connection():
    database_url = get_database_url()
    return psycopg.connect(database_url, row_factory=dict_row)


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tracked_bills (
                    id SERIAL PRIMARY KEY,
                    user_email TEXT NOT NULL,
                    bill_number TEXT NOT NULL,
                    bill_text TEXT,
                    agenda_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    UNIQUE(user_email, bill_number)
                )
            """)
        conn.commit()


def save_selected_bills(user_email, agenda_url, selected_items):
    with get_connection() as conn:
        with conn.cursor() as cur:
            for item in selected_items:
                cur.execute("""
                    INSERT INTO tracked_bills (
                        user_email,
                        bill_number,
                        bill_text,
                        agenda_url,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (user_email, bill_number)
                    DO UPDATE SET
                        bill_text = EXCLUDED.bill_text,
                        agenda_url = EXCLUDED.agenda_url,
                        is_active = TRUE,
                        created_at = CURRENT_TIMESTAMP
                """, (
                    user_email.strip().lower(),
                    item["bill_number"].strip().upper(),
                    item["text"],
                    agenda_url.strip()
                ))
        conn.commit()


def get_active_bills_for_user(user_email):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    user_email,
                    bill_number,
                    bill_text,
                    agenda_url,
                    created_at,
                    is_active
                FROM tracked_bills
                WHERE user_email = %s
                  AND is_active = TRUE
                ORDER BY bill_number
            """, (user_email.strip().lower(),))
            rows = cur.fetchall()

    return rows


def get_all_active_bills_grouped():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    user_email,
                    bill_number,
                    bill_text,
                    agenda_url
                FROM tracked_bills
                WHERE is_active = TRUE
                ORDER BY user_email, bill_number
            """)
            rows = cur.fetchall()

    grouped = {}
    for row in rows:
        email = row["user_email"]
        if email not in grouped:
            grouped[email] = {}
        grouped[email][row["bill_number"]] = row["bill_text"]

    return grouped


def deactivate_bill_for_user(user_email, bill_number):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tracked_bills
                SET is_active = FALSE
                WHERE user_email = %s
                  AND bill_number = %s
            """, (
                user_email.strip().lower(),
                bill_number.strip().upper()
            ))
        conn.commit()