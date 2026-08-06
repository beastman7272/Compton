from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import STORAGE_ROOT
from app.models import ImportItem, StoredPdf


DEFAULT_STORAGE_ROOT = STORAGE_ROOT
SPLIT_PDF_MERGE_MIN_FILES = 25
SPLIT_PDF_SINGLE_PAGE_RATIO = 0.85


@dataclass(slots=True)
class ZipPdfCandidate:
    """
    PDF entry metadata used to detect ConstructConnect split-page exports.
    """

    info: zipfile.ZipInfo
    page_count: int


def safe_name(value: str, max_length: int = 120) -> str:
    """
    Convert project/file names into safe filesystem names.
    """
    value = (value or "").strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ._")
    return value[:max_length] or "unnamed"


def file_sha256(path: Path) -> str:
    """
    Compute a SHA-256 hash for duplicate detection.
    """
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def project_storage_dir(
    storage_root: Path,
    project_id: int,
) -> Path:
    """
    App-owned storage directory for a project.
    """
    return storage_root / f"project_{project_id}"


def ensure_project_storage_dirs(
    storage_root: Path,
    project_id: int,
) -> dict[str, Path]:
    """
    Create the standard project storage directories.
    """
    base = project_storage_dir(storage_root, project_id)

    dirs = {
        "base": base,
        "originals": base / "originals",
        "documents": base / "documents",
        "attachments": base / "attachments",
        "extracted": base / "extracted",
    }

    for folder in dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    return dirs


def copy_original_download(
    matched_file: Path,
    storage_root: Path,
    project_id: int,
) -> Path:
    """
    Copy the original downloaded ZIP/PDF into app-owned storage.

    The app should not rely on files remaining in ~/Downloads.
    """
    matched_file = Path(matched_file)
    dirs = ensure_project_storage_dirs(storage_root, project_id)

    target = dirs["originals"] / safe_name(matched_file.name, max_length=180)

    if target.exists():
        target = unique_path(target)

    shutil.copy2(matched_file, target)
    return target


def unique_path(path: Path) -> Path:
    """
    Return a non-conflicting path by adding _002, _003, etc.
    """
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter:03d}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def natural_sort_key(value: str) -> list[int | str]:
    """
    Sort filenames with embedded numbers in human page order.
    """
    parts = re.split(r"(\d+)", value.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def extract_pdf_files(
    source_file: Path,
    storage_root: Path,
    project_id: int,
) -> list[Path]:
    """
    Given a downloaded PDF or ZIP, return PDF files ready to become uploads.

    - PDF: copy into documents/
    - ZIP: extract PDFs into extracted/, then copy them into documents/
    """
    source_file = Path(source_file)
    suffix = source_file.suffix.lower()

    if suffix == ".pdf":
        return [copy_pdf_to_documents(source_file, storage_root, project_id)]

    if suffix == ".zip":
        return extract_zip_pdfs(source_file, storage_root, project_id)

    raise ValueError(f"Unsupported import file type: {source_file.name}")


def copy_pdf_to_documents(
    pdf_path: Path,
    storage_root: Path,
    project_id: int,
) -> Path:
    """
    Copy a PDF into the project's documents directory.
    """
    dirs = ensure_project_storage_dirs(storage_root, project_id)

    target = dirs["documents"] / safe_name(pdf_path.name, max_length=180)

    if target.exists():
        target = unique_path(target)

    shutil.copy2(pdf_path, target)
    return target


def extract_zip_pdfs(
    zip_path: Path,
    storage_root: Path,
    project_id: int,
) -> list[Path]:
    """
    Copy PDFs from a ZIP into documents/.

    Large ConstructConnect plan exports sometimes contain one PDF per page.
    When detected, combine those page PDFs into multi-page PDFs first so the
    app creates a manageable number of upload records.

    Returns paths to the app-owned PDF copies in documents/.
    """
    dirs = ensure_project_storage_dirs(storage_root, project_id)

    stored_pdfs: list[Path] = []

    with zipfile.ZipFile(zip_path, "r") as z:
        pdfs = sorted(
            (
                info for info in z.infolist()
                if not info.is_dir() and Path(info.filename).suffix.lower() == ".pdf"
            ),
            key=lambda info: natural_sort_key(info.filename),
        )

        if should_merge_zip_pdfs(z, pdfs):
            return merge_zip_pdfs_by_folder(
                z=z,
                zip_path=zip_path,
                pdfs=pdfs,
                documents_dir=dirs["documents"],
                extracted_dir=dirs["extracted"],
            )

        for idx, pdf in enumerate(pdfs, start=1):
            target_name = f"{idx:03d}_{safe_name(Path(pdf.filename).name, max_length=170)}"
            target = dirs["documents"] / target_name

            if target.exists():
                target = unique_path(target)

            with z.open(pdf, "r") as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)

            stored_pdfs.append(target)

    if not stored_pdfs:
        raise RuntimeError(f"No PDF files found inside ZIP: {zip_path.name}")

    return stored_pdfs


def should_merge_zip_pdfs(
    z: zipfile.ZipFile,
    pdfs: list[zipfile.ZipInfo],
) -> bool:
    """
    Return True when a ZIP appears to be a split-page PDF export.
    """
    if len(pdfs) < SPLIT_PDF_MERGE_MIN_FILES:
        return False

    candidates = get_zip_pdf_candidates(z, pdfs)

    if not candidates:
        return False

    single_page_count = sum(1 for candidate in candidates if candidate.page_count == 1)
    return single_page_count / len(candidates) >= SPLIT_PDF_SINGLE_PAGE_RATIO


def get_zip_pdf_candidates(
    z: zipfile.ZipFile,
    pdfs: list[zipfile.ZipInfo],
) -> list[ZipPdfCandidate]:
    """
    Read PDF page counts directly from ZIP entries.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: PyMuPDF. Install it with: pip install pymupdf"
        ) from exc

    candidates: list[ZipPdfCandidate] = []

    for pdf in pdfs:
        with z.open(pdf, "r") as source:
            pdf_bytes = source.read()

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            candidates.append(
                ZipPdfCandidate(
                    info=pdf,
                    page_count=doc.page_count,
                )
            )

    return candidates


def merge_zip_pdfs_by_folder(
    z: zipfile.ZipFile,
    zip_path: Path,
    pdfs: list[zipfile.ZipInfo],
    documents_dir: Path,
    extracted_dir: Path,
) -> list[Path]:
    """
    Merge split-page PDFs into one combined PDF per logical ZIP folder.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: PyMuPDF. Install it with: pip install pymupdf"
        ) from exc

    grouped_pdfs = group_zip_pdfs_by_folder(zip_path, pdfs)
    stored_pdfs: list[Path] = []
    manifest_rows: list[dict[str, int | str]] = []

    for group_idx, (group_name, group_pdfs) in enumerate(grouped_pdfs.items(), start=1):
        merged_doc = fitz.open()
        page_cursor = 1
        group_manifest_rows: list[dict[str, int | str]] = []

        try:
            for pdf in group_pdfs:
                with z.open(pdf, "r") as source:
                    pdf_bytes = source.read()

                with fitz.open(stream=pdf_bytes, filetype="pdf") as source_doc:
                    start_page = page_cursor
                    merged_doc.insert_pdf(source_doc)
                    page_cursor += source_doc.page_count

                group_manifest_rows.append(
                    {
                        "combined_start_page": start_page,
                        "combined_end_page": page_cursor - 1,
                        "source_pdf": pdf.filename,
                    }
                )

            target_name = f"{group_idx:03d}_{safe_name(group_name, max_length=150)}.pdf"
            target = unique_path(documents_dir / target_name)
            merged_doc.save(target)
            stored_pdfs.append(target)
            manifest_rows.extend(
                {
                    "combined_pdf": target.name,
                    **row,
                }
                for row in group_manifest_rows
            )
        finally:
            merged_doc.close()

    write_merge_manifest(
        extracted_dir=extracted_dir,
        zip_path=zip_path,
        rows=manifest_rows,
    )

    return stored_pdfs


def group_zip_pdfs_by_folder(
    zip_path: Path,
    pdfs: list[zipfile.ZipInfo],
) -> dict[str, list[zipfile.ZipInfo]]:
    """
    Group PDFs by the most useful top-level ZIP folder.
    """
    split_paths = [split_zip_path(pdf.filename) for pdf in pdfs]
    group_index = 0

    if (
        split_paths
        and all(len(parts) > 2 for parts in split_paths)
        and len({parts[0].lower() for parts in split_paths}) == 1
    ):
        group_index = 1

    grouped: dict[str, list[zipfile.ZipInfo]] = {}

    for pdf, parts in zip(pdfs, split_paths):
        if len(parts) > group_index + 1:
            group_name = parts[group_index]
        else:
            group_name = zip_path.stem

        grouped.setdefault(group_name, []).append(pdf)

    return grouped


def split_zip_path(filename: str) -> list[str]:
    """
    Split ZIP paths from either slash style and drop empty path parts.
    """
    return [part for part in re.split(r"[\\/]+", filename) if part]


def write_merge_manifest(
    extracted_dir: Path,
    zip_path: Path,
    rows: list[dict[str, int | str]],
) -> None:
    """
    Store source-to-combined page mapping for troubleshooting.
    """
    if not rows:
        return

    manifest_path = unique_path(
        extracted_dir / f"{safe_name(zip_path.stem, max_length=120)}_merged_manifest.json"
    )

    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def build_stored_pdf(
    original_pdf_path: Path,
    stored_pdf_path: Path,
) -> StoredPdf:
    """
    Build a StoredPdf model from a copied PDF.
    """
    return StoredPdf(
        original_filename=original_pdf_path.name,
        stored_filename=stored_pdf_path.name,
        stored_path=stored_pdf_path,
        file_hash=file_sha256(stored_pdf_path),
        page_count=None,
    )


def prepare_import_files(
    item: ImportItem,
    matched_file: Path,
    storage_root: Path,
    project_id: int,
) -> tuple[Path, list[StoredPdf]]:
    """
    Copy the original downloaded file into app storage, then prepare PDF upload files.

    Returns:
        original_copy_path, stored_pdfs
    """
    storage_root = Path(storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)

    matched_file = Path(matched_file)

    if not matched_file.exists():
        raise FileNotFoundError(f"Matched file does not exist: {matched_file}")

    original_copy = copy_original_download(
        matched_file=matched_file,
        storage_root=storage_root,
        project_id=project_id,
    )

    pdf_paths = extract_pdf_files(
        source_file=original_copy,
        storage_root=storage_root,
        project_id=project_id,
    )

    stored_pdfs = [
        StoredPdf(
            original_filename=matched_file.name,
            stored_filename=pdf_path.name,
            stored_path=pdf_path,
            file_hash=file_sha256(pdf_path),
            page_count=None,
        )
        for pdf_path in pdf_paths
    ]

    return original_copy, stored_pdfs


def delete_project_storage(
    storage_root: Path,
    project_id: int,
) -> None:
    """
    Delete all local files for a project.

    This should be called only after user confirmation in the app.
    """
    folder = project_storage_dir(storage_root, project_id)

    if folder.exists():
        shutil.rmtree(folder)