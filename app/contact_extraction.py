from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

# Ensure local package imports work when running this file directly from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_connection
from app.pdf_text import extract_first_pages_text


CONTACT_KEYWORDS = [
    "architect",
#    "architecture",
#    "engineer",
#    "engineering",
    "general contractor",
    "contractor",
    "construction manager",
#    "owner",
#    "bid contact",
    "project manager",
#    "procurement",
#    "purchasing",
#    "contact",
]

CONTACT_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contact_type": {
                        "type": "string",
                        "enum": [
                            "Architect",
                            "General Contractor",
                            "Engineer",
                            "Construction Manager",
                            "Owner",
                            "Bid Contact",
                            "Other",
                        ],
                    },
                    "organization": {"type": "string"},
                    "contact_name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": [
                    "contact_type",
                    "organization",
                    "contact_name",
                    "email",
                    "phone",
                    "confidence",
                ],
            },
        },
    },
    "required": ["contacts"],
}


@dataclass(slots=True)
class ExtractedContact:
    contact_type: str
    organization: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    source_page_number: int | None = None
    confidence: float | None = None


def page_has_contact_keywords(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(keyword in text_lower for keyword in CONTACT_KEYWORDS)


def likely_contact_pages(
    pdf_path: Path | str,
    upload_id: int,
    max_pages: int = 10,
):
    """
    Return first-page-range PDF pages that look likely to contain
    Architect / GC / owner / bid contact information.
    """
    result = extract_first_pages_text(
        pdf_path=pdf_path,
        upload_id=upload_id,
        max_pages=max_pages,
    )

    return [
        page
        for page in result.pages
        if page.text and page_has_contact_keywords(page.text)
    ]


def extract_contacts_from_text_with_openai(
    page_text: str,
    page_number: int,
) -> list[ExtractedContact]:
    """
    Extract structured contact suggestions from one page of text.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError("Missing dependency. Run: pip install openai") from exc

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set.")

    client = OpenAI()

    prompt = f"""
Page number: {page_number}

Page text:
{page_text[:12000]}
"""

    response = client.responses.create(
        model="gpt-5.4-mini",
        instructions=(
            "Extract project contact information from the supplied page text. "
            "Only include contacts that actually appear in the text. Do not guess. "
            "Prefer organizations over individuals when the individual name is unclear. "
            "Use an empty string for unknown string fields. If no contacts are found, "
            "return an empty contacts array."
        ),
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "project_contacts",
                "schema": CONTACT_EXTRACTION_SCHEMA,
                "strict": True,
            }
        },
    )

    raw = response.output_text.strip()
    if not raw:
        raise RuntimeError(
            "OpenAI returned an empty text response. "
            f"{openai_response_debug_summary(response)}"
        )

    try:
        data = json.loads(raw)
    except JSONDecodeError as exc:
        raise RuntimeError(
            "OpenAI returned invalid JSON for contact extraction. "
            f"{openai_response_debug_summary(response)} "
            f"Raw response preview: {raw[:500]!r}"
        ) from exc

    contacts: list[ExtractedContact] = []

    for item in data.get("contacts", []):
        contacts.append(
            ExtractedContact(
                contact_type=str(item.get("contact_type", "Other")).strip() or "Other",
                organization=str(item.get("organization", "")).strip(),
                contact_name=str(item.get("contact_name", "")).strip(),
                email=str(item.get("email", "")).strip(),
                phone=str(item.get("phone", "")).strip(),
                source_page_number=page_number,
                confidence=float(item.get("confidence", 0.0) or 0.0),
            )
        )

    return contacts


def openai_response_debug_summary(response: Any) -> str:
    """
    Keep enough metadata to diagnose empty/invalid OpenAI responses without
    dumping the full PDF page text or response object.
    """
    status = getattr(response, "status", None)
    incomplete_details = getattr(response, "incomplete_details", None)
    output = getattr(response, "output", None) or []
    output_types = [
        str(getattr(item, "type", type(item).__name__))
        for item in output
    ]

    return (
        f"status={status!r}; "
        f"incomplete_details={incomplete_details!r}; "
        f"output_types={output_types!r}"
    )


def delete_existing_contacts_for_upload(
    conn: sqlite3.Connection,
    upload_id: int,
) -> None:
    conn.execute(
        """
        DELETE FROM project_contacts
        WHERE source_upload_id = ?
        """,
        (upload_id,),
    )


def insert_project_contact(
    conn: sqlite3.Connection,
    project_id: int,
    upload_id: int,
    contact: ExtractedContact,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO project_contacts (
            project_id,
            contact_type,
            organization,
            contact_name,
            email,
            phone,
            source_upload_id,
            source_page_number,
            confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            contact.contact_type,
            contact.organization,
            contact.contact_name,
            contact.email,
            contact.phone,
            upload_id,
            contact.source_page_number,
            contact.confidence,
        ),
    )

    return int(cur.lastrowid)


def extract_contacts_for_upload(
    conn: sqlite3.Connection,
    upload_id: int,
    max_pages: int = 10,
) -> list[int]:
    """
    Extract and store contact suggestions for one uploaded PDF.
    """
    upload = conn.execute(
        """
        SELECT id, project_id, stored_path
        FROM uploads
        WHERE id = ?
        """,
        (upload_id,),
    ).fetchone()

    if not upload:
        raise RuntimeError(f"Upload not found: {upload_id}")

    pdf_path = Path(upload["stored_path"])

    if not pdf_path.exists():
        raise FileNotFoundError(f"Stored PDF not found: {pdf_path}")

    pages = likely_contact_pages(
        pdf_path=pdf_path,
        upload_id=upload_id,
        max_pages=max_pages,
    )

    if not pages:
        return []

    delete_existing_contacts_for_upload(conn, upload_id)

    inserted_ids: list[int] = []

    for page in pages:
        contacts = extract_contacts_from_text_with_openai(
            page_text=page.text,
            page_number=page.page_number,
        )

        for contact in contacts:
            inserted_id = insert_project_contact(
                conn=conn,
                project_id=int(upload["project_id"]),
                upload_id=upload_id,
                contact=contact,
            )
            inserted_ids.append(inserted_id)

    return inserted_ids


def extract_contacts_for_upload_ids(
    upload_ids: list[int],
    db_path: Path | str = Path("data") / "cqe.db",
    max_pages: int = 10,
) -> None:
    """
    Run contact extraction for a specific list of upload IDs.
    """
    if not upload_ids:
        return

    with get_connection(db_path) as conn:
        for upload_id in upload_ids:
            try:
                inserted_ids = extract_contacts_for_upload(
                    conn=conn,
                    upload_id=upload_id,
                    max_pages=max_pages,
                )
                conn.commit()
                print(
                    f"Contact extraction for upload {upload_id}: "
                    f"{len(inserted_ids)} contact(s)"
                )
            except Exception as exc:
                conn.commit()
                print(f"ERROR extracting contacts for upload {upload_id}: {exc}")


def extract_contacts_for_indexed_uploads(
    db_path: Path | str = Path("data") / "cqe.db",
    max_uploads: int | None = None,
    max_pages: int = 10,
) -> None:
    """
    Convenience runner for already-indexed uploads.
    """
    with get_connection(db_path) as conn:
        query = """
            SELECT id
            FROM uploads
            WHERE upload_status = 'indexed'
            ORDER BY created_at
        """

        params: tuple[Any, ...] = ()

        if max_uploads is not None:
            query += " LIMIT ?"
            params = (max_uploads,)

        rows = conn.execute(query, params).fetchall()

        if not rows:
            print("No indexed uploads found for contact extraction.")
            return

        for row in rows:
            upload_id = int(row["id"])

            try:
                inserted_ids = extract_contacts_for_upload(
                    conn=conn,
                    upload_id=upload_id,
                    max_pages=max_pages,
                )
                conn.commit()
                print(f"Upload {upload_id}: {len(inserted_ids)} contact(s)")
            except Exception as exc:
                conn.commit()
                print(f"ERROR extracting contacts for upload {upload_id}: {exc}")


if __name__ == "__main__":
    extract_contacts_for_indexed_uploads()