from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SourceSystem = Literal["BuildingConnected", "ConstructConnect", "Manual"]

ImportStatus = Literal[
    "imported",
    "updated",
    "skipped",
    "file_not_found",
    "duplicate_file",
    "error",
]

ProjectStatus = Literal[
    "Needs Review",
    "Not Pursuing",
    "Pursuing",
    "Submitted",
    "Won",
    "Lost",
]

UploadStatus = Literal[
    "pending_index",
    "indexed",
    "index_error",
    "not_indexed",
]


@dataclass(slots=True)
class ImportItem:
    """
    Normalized input row from either the BuildingConnected Bid Board sheet
    or the ConstructConnect sheet.

    The goal is for the importer to work with this common shape instead of
    knowing each Google Sheet's column layout.
    """

    source_system: SourceSystem

    source_sheet_id: str
    source_tab: str
    source_row: int

    project_name: str

    source_project_id: str = ""
    address_raw: str = ""
    city: str = ""
    state: str = ""
    county: str = ""

    bid_date: str = ""
    budget: str = ""
    category_or_scope: str = ""

    downloaded_status: str = ""
    import_status: str = ""


@dataclass(slots=True)
class ImportResult:
    """
    Result returned by the importer after trying to create/update a project
    and add one or more upload records.
    """

    status: ImportStatus
    message: str

    project_id: int | None = None
    created_project: bool = False
    matched_existing_project: bool = False

    uploaded_file_ids: list[int] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"imported", "updated", "duplicate_file"}


@dataclass(slots=True)
class StoredPdf:
    """
    Represents a PDF copied into app-owned local storage.
    """

    original_filename: str
    stored_filename: str
    stored_path: Path
    file_hash: str
    page_count: int | None = None


@dataclass(slots=True)
class ProjectMatch:
    """
    Internal result from project deduplication.
    """

    project_id: int
    confidence: float
    reason: str


@dataclass(slots=True)
class SearchFilter:
    id: int
    name: str
    category: str
    is_active: bool = True


@dataclass(slots=True)
class SearchTerm:
    id: int
    filter_id: int
    term: str


@dataclass(slots=True)
class SearchResult:
    project_id: int
    upload_id: int
    filter_id: int
    term_id: int
    page_number: int
    matched_text: str
    context_text: str = ""


@dataclass(slots=True)
class ProjectContact:
    project_id: int
    contact_type: str
    organization: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    source_upload_id: int | None = None
    source_page_number: int | None = None
    confidence: float | None = None