from __future__ import annotations

import argparse
import base64
from datetime import datetime
import io
import os
from pathlib import Path

import pandas as pd

from app.google_runtime import load_oauth_credentials

# --- Config ---
def env_value(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


SPREADSHEET_ID = env_value("CONSTRUCTCONNECT_SHEET_ID", "1vqEd71BGHNMDJdBcymM4cgGzEQhlXsib3sFXY9Qlt7U")

EMAIL_FROM = "info@comptonsales.com"

MANUFACTURER_DAYS = {
    "Citadel": {"monday"},
    "Fortress": {"tuesday", "sunday"},
    "Fabral": {"wednesday", "saturday"},
    "Metal-Era": {"thursday"},
}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

EXCEL_EXTENSIONS = (".xls", ".xlsx", ".xlsm")

SKIP_BRANDS = []
# SKIP_BRANDS = [
#     "advance auto parts", "aldi", "autozone", "barnes & noble", "bass pro shops", "bath & body works", "best western", 
#     "boa", "bojangles", "chick-fil-a", "circle k", "cvs", "dollar tree", "dutch bros", "fifth third bank",
#     "five guys", "floor & decor", "harbor freight", "holiday inn", "home 2 suites", "hyatt", "insomnia cookies", "kia", 
#     "kroger", "lidl", "long john silvers", "members exchange", "officemax", "old navy", "o'reilly", "pilot", "prequal",
#     "publix", "sam's club", "savers", "smuckers", "speedway", "sprouts", "staybridge suites", "ta travel center", 
#     "total wine & more", "toyota", "tractor supply", "u-haul", "us bank", "usps", "valvoline", "victoria's secret", 
#     "visionworks", "wawa", "walmart", "whole foods", "wingstop", "winn-dixie", "wm", "xfinity", "zaxby"
# ]


def get_creds():
    # Gmail access must use OAuth user credentials. This token also writes to
    # Sheets because this Gmail workflow appends imported rows.
    return load_oauth_credentials(
        SCOPES,
        token_filename="token.json",
        use_file_env=False,
    )


def normalize_text(value: object) -> str:
    return (
        str(value or "")
        .lower()
        .replace("’", "'")
        .replace("&", "and")
        .strip()
    )


def should_skip_project_title(title: object) -> bool:
    normalized_title = normalize_text(title)

    for brand in SKIP_BRANDS:
        normalized_brand = normalize_text(brand)
        if normalized_brand in normalized_title:
            return True

    return False


def normalize_column_name(column: object) -> str:
    return "".join(char for char in str(column or "").lower() if char.isalnum())


def column_values(df: pd.DataFrame, *names: str, required: bool = True):
    normalized_columns = {normalize_column_name(column): column for column in df.columns}

    for name in names:
        column = normalized_columns.get(normalize_column_name(name))
        if column is not None:
            return df[column]

    if required:
        expected = " or ".join(names)
        available = ", ".join(str(column) for column in df.columns)
        raise KeyError(f"Missing required Excel column '{expected}'. Available columns: {available}")

    return ""


def sheet_range(tab_name: str, range_name: str) -> str:
    escaped_tab_name = tab_name.replace("'", "''")
    return f"'{escaped_tab_name}'!{range_name}"


def scheduled_manufacturers(run_day: str) -> list[str]:
    return [
        manufacturer
        for manufacturer, days in MANUFACTURER_DAYS.items()
        if run_day in days
    ]


def find_matching_messages(gmail_service, subject_text: str) -> list[str]:
    # For local testing, forwarded emails may come from different accounts.
    # Add back f"from:{EMAIL_FROM} " when matching against original production senders.
    queries = [
        f'subject:"{subject_text}" is:unread newer_than:1d has:attachment',
        f"subject:{subject_text} is:unread newer_than:1d has:attachment",
        f'"{subject_text}" is:unread newer_than:1d has:attachment',
    ]

    for query in queries:
        result = gmail_service.users().messages().list(userId="me", q=query, maxResults=10).execute()
        messages = result.get("messages", [])
        print(f"Gmail search matched {len(messages)} message(s): {query}")
        if messages:
            return [message["id"] for message in messages]

    return []


def mark_as_read(gmail_service, message_id: str) -> None:
    gmail_service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def walk_parts_for_attachment(parts):
    for part in parts or []:
        filename = part.get("filename", "")
        body = part.get("body", {})
        mime_type = part.get("mimeType", "")

        if filename and filename.lower().endswith(EXCEL_EXTENSIONS):
            attachment_id = body.get("attachmentId")
            if attachment_id:
                return filename, attachment_id

        # recurse into nested parts
        nested = part.get("parts")
        if nested:
            found = walk_parts_for_attachment(nested)
            if found:
                return found

    return None


def download_excel_attachment(gmail_service, message_id: str) -> tuple[str, bytes]:
    message = gmail_service.users().messages().get(userId="me", id=message_id).execute()
    payload = message.get("payload", {})
    parts = payload.get("parts", [])

    found = walk_parts_for_attachment(parts)
    if not found:
        raise RuntimeError("No Excel attachment found on the matching email.")

    filename, attachment_id = found

    attachment = (
        gmail_service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )

    data = attachment.get("data")
    if not data:
        raise RuntimeError("Attachment data was empty.")

    file_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
    return filename, file_bytes


def load_excel_rows(file_bytes: bytes, filename: str) -> list[list]:
    suffix = Path(filename).suffix.lower()

    if suffix == ".xls":
        df = pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
    else:
        df = pd.read_excel(io.BytesIO(file_bytes))

    df.columns = [str(column).strip() for column in df.columns]

    # Drop completely empty rows
    df = df.dropna(how="all")

    # Skip national brands that are not good bid targets
    df = df[~column_values(df, "Project Title").apply(should_skip_project_title)]

    # Keep only the fields we want, in the order we want
    df = pd.DataFrame({
        "Project ID": column_values(df, "Project ID"),
        "Project Title": column_values(df, "Project Title"),
        "City": column_values(df, "City"),
        "State/Province": column_values(df, "State/Province", "State Province"),
        "County": column_values(df, "County"),
        "Bid Date": column_values(df, "Bid Date"),
        "Stage": column_values(df, "Stage"),
        "Project Value": column_values(df, "Project Value"),
        "Update Date": column_values(df, "Update Date"),
        "Work Type": column_values(df, "Work Type"),
        "Subcategory": column_values(df, "SubCategory", "Subcategory", "Sub Category", required=False),
    })

    # Convert NaN to empty strings for Sheets append
    df = df.fillna("")

    def clean_cell(x):
        if pd.isna(x):
            return ""
        if isinstance(x, pd.Timestamp):
            return x.strftime("%Y-%m-%d")
        return x

    df = df.map(clean_cell)

    # Append data rows only; not header row
    rows = df.values.tolist()

    if not rows:
        raise RuntimeError("Excel file contained no data rows.")

    return rows


def append_to_sheet(sheets_service, tab_name: str, rows: list[list]) -> None:
    body = {"values": rows}

    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=sheet_range(tab_name, "A:A"),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


def process_manufacturer(gmail_service, sheets_service, manufacturer: str) -> int:
    message_ids = find_matching_messages(gmail_service, manufacturer)
    if not message_ids:
        print(f"No unread {manufacturer} email found in the last day.")
        return 0

    appended_count = 0
    for message_id in message_ids:
        filename, file_bytes = download_excel_attachment(gmail_service, message_id)
        rows = load_excel_rows(file_bytes, filename)
        append_to_sheet(sheets_service, manufacturer, rows)
        mark_as_read(gmail_service, message_id)
        appended_count += len(rows)
        print(f"Done. Appended {len(rows)} rows from {filename} to tab '{manufacturer}'.")

    return appended_count


def parse_args():
    parser = argparse.ArgumentParser(description="Process ConstructConnect manufacturer emails into Sheets")
    parser.add_argument(
        "--run-day",
        choices=WEEKDAYS,
        default=datetime.now().strftime("%A").lower(),
        help="Pretend today is this weekday for manual testing",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    manufacturers = scheduled_manufacturers(args.run_day)

    if not manufacturers:
        print(f"No ConstructConnect manufacturers scheduled for {args.run_day.title()}.")
        return

    creds = get_creds()
    from googleapiclient.discovery import build

    gmail_service = build("gmail", "v1", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)

    total_rows = 0
    print(f"Processing {args.run_day.title()} manufacturers: {', '.join(manufacturers)}")

    for manufacturer in manufacturers:
        total_rows += process_manufacturer(gmail_service, sheets_service, manufacturer)

    print(f"Finished. Appended {total_rows} total row(s).")


if __name__ == "__main__":
    main()