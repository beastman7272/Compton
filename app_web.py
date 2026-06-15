from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Flask, render_template, abort, send_file, url_for, redirect, request
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.db import get_connection
from app.importer import import_project_from_source
from app.models import ImportItem

import re


DB_PATH = Path("data") / "cqe.db"
STORAGE_ROOT = Path("data") / "uploads"
ALLOWED_MANUAL_UPLOAD_EXTENSIONS = {".pdf", ".zip"}

PROJECT_STATUSES = [
    "Needs Review",
    "Not Pursuing",
    "Pursuing",
    "Submitted",
    "Won",
    "Lost",
]

PROJECT_CONTACT_TYPES = {
    "architect": "Architect",
    "general-contractor": "General Contractor",
}


def blank_project_contact(contact_type: str) -> dict:
    return {
        "id": None,
        "contact_type": contact_type,
        "organization": "",
        "contact_name": "",
        "email": "",
        "phone": "",
        "source_upload_id": None,
        "source_page_number": None,
        "confidence": None,
    }


def contact_sort_key(contact) -> tuple:
    confidence = float(contact["confidence"] or 0.0)
    completeness = sum(
        1
        for field in ("organization", "contact_name", "email", "phone")
        if (contact[field] or "").strip()
    )
    manual_priority = 1 if contact["source_upload_id"] is None else 0

    return (confidence, completeness, manual_priority)


def best_project_contacts(contacts) -> dict[str, object]:
    selected = {
        contact_type: blank_project_contact(contact_type)
        for contact_type in PROJECT_CONTACT_TYPES.values()
    }

    for contact_type in PROJECT_CONTACT_TYPES.values():
        matching_contacts = [
            contact
            for contact in contacts
            if contact["contact_type"] == contact_type
        ]

        if matching_contacts:
            selected[contact_type] = max(matching_contacts, key=contact_sort_key)

    return selected


def add_match_to_group(match_groups: list[dict], group_lookup: dict, row) -> None:
    key = (
        row["category"],
        row["filter_name"],
        row["term"],
        row["upload_id"],
        row["stored_filename"],
    )

    if key not in group_lookup:
        group_lookup[key] = {
            "category": row["category"],
            "filter_name": row["filter_name"],
            "term": row["term"],
            "upload_id": row["upload_id"],
            "stored_filename": row["stored_filename"],
            "pages": [],
            "_seen_pages": set(),
        }
        match_groups.append(group_lookup[key])

    group = group_lookup[key]
    page_number = row["page_number"]

    if page_number not in group["_seen_pages"]:
        group["pages"].append(
            {
                "page_number": page_number,
                "result_id": row["id"],
            }
        )
        group["_seen_pages"].add(page_number)


def finalize_match_groups(match_groups: list[dict]) -> list[dict]:
    for group in match_groups:
        group.pop("_seen_pages", None)

    return match_groups


def build_term_options(match_groups: list[dict]) -> list[dict]:
    options = []
    seen_terms = set()

    for group in match_groups:
        term = group["term"]
        key = term.casefold()

        if key in seen_terms:
            continue

        seen_terms.add(key)
        options.append({"term": term})

    return options


def build_filter_options(match_groups: list[dict]) -> list[dict]:
    options = []
    seen_filters = set()

    for group in match_groups:
        filter_name = group["filter_name"]
        key = filter_name.casefold()

        if key in seen_filters:
            continue

        seen_filters.add(key)
        options.append({"filter_name": filter_name})

    return options


def build_upload_options(match_groups: list[dict]) -> list[dict]:
    options = []
    seen_uploads = set()

    for group in match_groups:
        upload_id = group["upload_id"]

        if upload_id in seen_uploads:
            continue

        seen_uploads.add(upload_id)
        options.append(
            {
                "upload_id": upload_id,
                "stored_filename": group["stored_filename"],
            }
        )

    return options


def load_manual_project_choices():
    with get_connection(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT id, name, city, state
            FROM projects
            ORDER BY updated_at DESC, name
            """
        ).fetchall()


def manual_upload_filename(uploaded_file: FileStorage) -> str:
    filename = secure_filename(uploaded_file.filename or "")

    if not filename:
        raise ValueError("Choose a PDF or ZIP file to upload.")

    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_MANUAL_UPLOAD_EXTENSIONS:
        raise ValueError("Manual uploads must be a PDF or ZIP file.")

    return filename


def project_has_contact_value(project_id: int, contact_type: str) -> bool:
    with get_connection(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM project_contacts
            WHERE project_id = ?
              AND contact_type = ?
              AND (
                  COALESCE(organization, '') != ''
                  OR COALESCE(contact_name, '') != ''
                  OR COALESCE(email, '') != ''
                  OR COALESCE(phone, '') != ''
              )
            LIMIT 1
            """,
            (project_id, contact_type),
        ).fetchone()

    return row is not None


def project_missing_architect_and_gc(project_id: int) -> bool:
    return not project_has_contact_value(
        project_id,
        "Architect",
    ) and not project_has_contact_value(
        project_id,
        "General Contractor",
    )


def import_item_for_manual_new_project(form) -> ImportItem:
    return ImportItem(
        source_system="Manual",
        source_sheet_id="",
        source_tab="",
        source_row=0,
        project_name=form.get("project_name", "").strip(),
        address_raw=form.get("address_raw", "").strip(),
        city=form.get("city", "").strip(),
        state=form.get("state", "").strip(),
        bid_date=form.get("bid_date", "").strip(),
        budget=form.get("budget", "").strip(),
    )


def import_item_for_existing_project(project_row) -> ImportItem:
    return ImportItem(
        source_system="Manual",
        source_sheet_id="",
        source_tab="",
        source_row=0,
        project_name=project_row["name"],
        address_raw=project_row["address_raw"] or "",
        city=project_row["city"] or "",
        state=project_row["state"] or "",
        county=project_row["county"] or "",
        bid_date=project_row["bid_date"] or "",
        budget=project_row["budget"] or "",
    )


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def home():
        return dashboard()

    @app.route("/dashboard")
    def dashboard():
        status_filter = request.args.get("status", "").strip()

        if status_filter and status_filter not in PROJECT_STATUSES:
            abort(400, "Invalid status filter")

        where_sql = ""
        params = []

        if status_filter:
            where_sql = "WHERE p.status = ?"
            params.append(status_filter)

        with get_connection(DB_PATH) as conn:
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM projects
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()

            total_project_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM projects
                """
            ).fetchone()["count"]

            projects = conn.execute(
                f"""
                SELECT
                    p.id,
                    p.name,
                    p.city,
                    p.state,
                    p.status,
                    p.updated_at,
                    COUNT(DISTINCT u.id) AS upload_count,
                    COUNT(DISTINCT sr.id) AS match_count
                FROM projects p
                LEFT JOIN uploads u ON u.project_id = p.id
                LEFT JOIN search_results sr ON sr.project_id = p.id
                {where_sql}
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                """,
                params,
            ).fetchall()

            matches_by_project = {project["id"]: [] for project in projects}
            group_lookups_by_project = {project["id"]: {} for project in projects}
            project_ids = [project["id"] for project in projects]

            if project_ids:
                placeholders = ", ".join("?" for _ in project_ids)
                match_rows = conn.execute(
                    f"""
                    SELECT
                        sr.id,
                        sr.project_id,
                        sf.name AS filter_name,
                        sf.category,
                        st.term,
                        u.id AS upload_id,
                        u.stored_filename,
                        sr.page_number
                    FROM search_results sr
                    JOIN search_filters sf ON sf.id = sr.filter_id
                    JOIN search_terms st ON st.id = sr.term_id
                    JOIN uploads u ON u.id = sr.upload_id
                    WHERE sr.project_id IN ({placeholders})
                    ORDER BY sr.project_id, sf.category, sf.name, st.term, u.stored_filename, sr.page_number
                    """,
                    project_ids,
                ).fetchall()

                for match in match_rows:
                    project_id = match["project_id"]
                    add_match_to_group(
                        matches_by_project[project_id],
                        group_lookups_by_project[project_id],
                        match,
                    )

            for project_id, matches in matches_by_project.items():
                matches_by_project[project_id] = finalize_match_groups(matches)

        return render_template(
            "dashboard.html",
            status_rows=status_rows,
            total_project_count=total_project_count,
            projects=projects,
            matches_by_project=matches_by_project,
            active_status=status_filter,
            statuses=PROJECT_STATUSES,
        )

    @app.route("/search-results")
    def search_results():
        status_filter = request.args.get("status", "exclude_not_pursuing").strip()

        allowed_filters = {"all", "exclude_not_pursuing"} | set(PROJECT_STATUSES)

        if status_filter not in allowed_filters:
            abort(400, "Invalid status filter")

        where_sql = ""
        params = []

        if status_filter == "exclude_not_pursuing":
            where_sql = "WHERE p.status != ?"
            params.append("Not Pursuing")
        elif status_filter in PROJECT_STATUSES:
            where_sql = "WHERE p.status = ?"
            params.append(status_filter)

        with get_connection(DB_PATH) as conn:
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM projects
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()

            match_rows = conn.execute(
                f"""
                SELECT
                    sr.id,
                    p.updated_at AS updated,
                    p.state,
                    p.name AS project_name,
                    p.id AS project_id,
                    p.bid_date,
                    sf.name AS filter_name,
                    st.term AS term,
                    u.id AS upload_id,
                    u.stored_filename,
                    sr.page_number,
                    p.status,
                    sf.category,
                    sr.matched_text,
                    sr.context_text
                FROM search_results sr
                JOIN projects p ON p.id = sr.project_id
                JOIN uploads u ON u.id = sr.upload_id
                JOIN search_filters sf ON sf.id = sr.filter_id
                JOIN search_terms st ON st.id = sr.term_id
                {where_sql}
                ORDER BY p.updated_at DESC, p.state, p.name, sf.category, sf.name, st.term, u.stored_filename, sr.page_number
                """,
                params,
            ).fetchall()

            rows = []
            rows_by_project = {}
            group_lookups_by_project = {}

            for match in match_rows:
                project_id = match["project_id"]

                if project_id not in rows_by_project:
                    rows_by_project[project_id] = {
                        "updated": match["updated"],
                        "state": match["state"],
                        "project_name": match["project_name"],
                        "project_id": project_id,
                        "bid_date": match["bid_date"],
                        "status": match["status"],
                        "match_count": 0,
                        "matches": [],
                    }
                    group_lookups_by_project[project_id] = {}
                    rows.append(rows_by_project[project_id])

                project_row = rows_by_project[project_id]
                project_row["match_count"] += 1
                add_match_to_group(
                    project_row["matches"],
                    group_lookups_by_project[project_id],
                    match,
                )

            for row in rows:
                row["matches"] = finalize_match_groups(row["matches"])
                row["filter_options"] = build_filter_options(row["matches"])
                row["term_options"] = build_term_options(row["matches"])
                row["upload_options"] = build_upload_options(row["matches"])
                row["category"] = row["matches"][0]["category"] if row["matches"] else ""

        return render_template(
            "search_results.html",
            rows=rows,
            statuses=PROJECT_STATUSES,
            status_rows=status_rows,
            active_status_filter=status_filter,
        )

    @app.route("/projects")
    def projects():
        with get_connection(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT
                    p.id,
                    p.updated_at,
                    p.state,
                    p.name,
                    p.bid_date,
                    p.budget,
                    p.status,
                    COUNT(u.id) AS upload_count
                FROM projects p
                LEFT JOIN uploads u ON u.project_id = p.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                """
            ).fetchall()

        return render_template("projects.html", rows=rows)

    @app.route("/projects/manual", methods=["GET", "POST"])
    def manual_project_load():
        errors: list[str] = []
        form_data = {
            "load_mode": request.form.get("load_mode", "new"),
            "existing_project_id": request.form.get("existing_project_id", ""),
            "project_name": request.form.get("project_name", ""),
            "state": request.form.get("state", ""),
            "city": request.form.get("city", ""),
            "address_raw": request.form.get("address_raw", ""),
            "bid_date": request.form.get("bid_date", ""),
            "budget": request.form.get("budget", ""),
        }

        if request.method == "POST":
            load_mode = form_data["load_mode"]
            uploaded_files = [
                uploaded_file
                for uploaded_file in request.files.getlist("project_file")
                if uploaded_file and uploaded_file.filename
            ]
            validated_uploads: list[tuple[FileStorage, str]] = []
            item: ImportItem | None = None

            if load_mode not in {"new", "existing"}:
                errors.append("Choose whether to create a new project or attach to an existing project.")

            if not uploaded_files:
                errors.append("Choose one or more PDF or ZIP files to upload.")

            if load_mode == "existing":
                try:
                    existing_project_id = int(form_data["existing_project_id"])
                except ValueError:
                    errors.append("Choose an existing project.")
                    existing_project_id = 0

                if existing_project_id > 0:
                    with get_connection(DB_PATH) as conn:
                        existing_project = conn.execute(
                            """
                            SELECT *
                            FROM projects
                            WHERE id = ?
                            """,
                            (existing_project_id,),
                        ).fetchone()

                    if not existing_project:
                        errors.append("The selected existing project was not found.")
                    else:
                        item = import_item_for_existing_project(existing_project)
            elif load_mode == "new":
                project_name = form_data["project_name"].strip()
                state = form_data["state"].strip()

                if not project_name:
                    errors.append("Project name is required.")
                if not state:
                    errors.append("State is required.")

                item = import_item_for_manual_new_project(request.form)

            for uploaded_file in uploaded_files:
                try:
                    filename = manual_upload_filename(uploaded_file)
                except ValueError as exc:
                    errors.append(str(exc))
                else:
                    validated_uploads.append((uploaded_file, filename))

            if not errors and item and validated_uploads:
                uploaded_file_ids: list[int] = []
                project_id: int | None = None

                with tempfile.TemporaryDirectory() as temp_dir:
                    for index, (uploaded_file, filename) in enumerate(validated_uploads):
                        upload_dir = Path(temp_dir) / str(index)
                        upload_dir.mkdir()
                        temp_path = upload_dir / filename
                        uploaded_file.save(temp_path)

                        result = import_project_from_source(
                            item=item,
                            matched_file=temp_path,
                            db_path=DB_PATH,
                            storage_root=STORAGE_ROOT,
                        )

                        if result.project_id:
                            project_id = result.project_id

                        if result.uploaded_file_ids:
                            uploaded_file_ids.extend(result.uploaded_file_ids)

                        if not result.ok:
                            errors.append(f"{filename}: {result.message}")
                            errors.extend(result.errors)

                if uploaded_file_ids:
                    from app.search import index_upload_ids

                    index_upload_ids(
                        upload_ids=uploaded_file_ids,
                        db_path=DB_PATH,
                    )

                    if project_id and project_missing_architect_and_gc(project_id):
                        from app.contact_extraction import extract_contacts_for_upload_ids

                        extract_contacts_for_upload_ids(
                            upload_ids=uploaded_file_ids,
                            db_path=DB_PATH,
                        )

                if not errors:
                    if project_id:
                        return redirect(url_for("project_detail", project_id=project_id))

                    return redirect(url_for("projects"))

        return render_template(
            "manual_project_load.html",
            errors=errors,
            form_data=form_data,
            existing_projects=load_manual_project_choices(),
        )

    @app.route("/projects/delete", methods=["POST"])
    def delete_projects():
        project_ids: list[int] = []

        for raw_project_id in request.form.getlist("project_ids"):
            try:
                project_id = int(raw_project_id)
            except ValueError:
                abort(400, "Invalid project selection")

            if project_id <= 0:
                abort(400, "Invalid project selection")

            project_ids.append(project_id)

        if not project_ids:
            return redirect(url_for("projects"))

        project_ids = list(dict.fromkeys(project_ids))
        placeholders = ", ".join("?" for _ in project_ids)

        with get_connection(DB_PATH) as conn:
            existing_rows = conn.execute(
                f"""
                SELECT id
                FROM projects
                WHERE id IN ({placeholders})
                """,
                project_ids,
            ).fetchall()

            existing_project_ids = [row["id"] for row in existing_rows]

            if existing_project_ids:
                existing_placeholders = ", ".join("?" for _ in existing_project_ids)

                conn.execute(
                    f"""
                    DELETE FROM project_contacts
                    WHERE project_id IN ({existing_placeholders})
                    """,
                    existing_project_ids,
                )
                conn.execute(
                    f"""
                    DELETE FROM search_results
                    WHERE project_id IN ({existing_placeholders})
                    """,
                    existing_project_ids,
                )
                conn.execute(
                    f"""
                    DELETE FROM uploads
                    WHERE project_id IN ({existing_placeholders})
                    """,
                    existing_project_ids,
                )
                conn.execute(
                    f"""
                    DELETE FROM projects
                    WHERE id IN ({existing_placeholders})
                    """,
                    existing_project_ids,
                )
                conn.commit()

        return redirect(url_for("projects"))

    @app.route("/projects/<int:project_id>")
    def project_detail(project_id: int):
        with get_connection(DB_PATH) as conn:
            project = conn.execute(
                """
                SELECT *
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()

            if not project:
                return "Project not found", 404

            uploads = conn.execute(
                """
                SELECT *
                FROM uploads
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()

            contacts = conn.execute(
                """
                SELECT *
                FROM project_contacts
                WHERE project_id = ?
                ORDER BY contact_type, confidence DESC, organization
                """,
                (project_id,),
            ).fetchall()

            matches = conn.execute(
                """
                SELECT
                    sf.name AS filter_name,
                    sf.category,
                    st.term,
                    u.stored_filename,
                    sr.page_number,
                    sr.context_text
                FROM search_results sr
                JOIN search_filters sf ON sf.id = sr.filter_id
                JOIN search_terms st ON st.id = sr.term_id
                JOIN uploads u ON u.id = sr.upload_id
                WHERE sr.project_id = ?
                ORDER BY sf.category, sf.name, st.term, sr.page_number
                LIMIT 100
                """,
                (project_id,),
            ).fetchall()

        return render_template(
            "project_detail.html",
            project=project,
            uploads=uploads,
            selected_contacts=best_project_contacts(contacts),
            matches=matches,
            statuses=PROJECT_STATUSES,
        )
    
    @app.route("/projects/<int:project_id>/contacts/<contact_type_key>", methods=["POST"])
    def update_project_contact(project_id: int, contact_type_key: str):
        contact_type = PROJECT_CONTACT_TYPES.get(contact_type_key)

        if not contact_type:
            abort(400, "Invalid contact type")

        organization = request.form.get("organization", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        with get_connection(DB_PATH) as conn:
            project = conn.execute(
                """
                SELECT id
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()

            if not project:
                abort(404)

            existing_manual = conn.execute(
                """
                SELECT id
                FROM project_contacts
                WHERE project_id = ?
                  AND contact_type = ?
                  AND source_upload_id IS NULL
                ORDER BY id
                LIMIT 1
                """,
                (project_id, contact_type),
            ).fetchone()

            has_contact_value = any([organization, contact_name, email, phone])

            if not has_contact_value:
                conn.execute(
                    """
                    DELETE FROM project_contacts
                    WHERE project_id = ?
                      AND contact_type = ?
                      AND source_upload_id IS NULL
                    """,
                    (project_id, contact_type),
                )
            elif existing_manual:
                conn.execute(
                    """
                    UPDATE project_contacts
                    SET organization = ?,
                        contact_name = ?,
                        email = ?,
                        phone = ?,
                        confidence = 1.0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        organization,
                        contact_name,
                        email,
                        phone,
                        existing_manual["id"],
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM project_contacts
                    WHERE project_id = ?
                      AND contact_type = ?
                      AND source_upload_id IS NULL
                      AND id != ?
                    """,
                    (project_id, contact_type, existing_manual["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO project_contacts (
                        project_id,
                        contact_type,
                        organization,
                        contact_name,
                        email,
                        phone,
                        confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1.0)
                    """,
                    (
                        project_id,
                        contact_type,
                        organization,
                        contact_name,
                        email,
                        phone,
                    ),
                )

            conn.execute(
                """
                UPDATE projects
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (project_id,),
            )
            conn.commit()

        return redirect(url_for("project_detail", project_id=project_id))

    @app.route("/projects/<int:project_id>/status", methods=["POST"])
    def update_project_status(project_id: int):
        new_status = request.form.get("status", "").strip()
        next_url = request.form.get("next", url_for("project_detail", project_id=project_id))

        if new_status not in PROJECT_STATUSES:
            abort(400, "Invalid project status")

        with get_connection(DB_PATH) as conn:
            project = conn.execute(
                """
                SELECT id
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()

            if not project:
                abort(404)

            conn.execute(
                """
                UPDATE projects
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_status, project_id),
            )
            conn.commit()

        return redirect(next_url)

    @app.route("/uploads/<int:upload_id>/view")
    def view_upload(upload_id: int):
        with get_connection(DB_PATH) as conn:
            upload = conn.execute(
                """
                SELECT id, stored_path, stored_filename
                FROM uploads
                WHERE id = ?
                """,
                (upload_id,),
            ).fetchone()

        if not upload:
            abort(404)

        file_path = Path(upload["stored_path"])

        if not file_path.exists():
            abort(404)

        return send_file(
            file_path,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=upload["stored_filename"],
        )


    @app.route("/uploads/<int:upload_id>/download")
    def download_upload(upload_id: int):
        with get_connection(DB_PATH) as conn:
            upload = conn.execute(
                """
                SELECT id, stored_path, stored_filename
                FROM uploads
                WHERE id = ?
                """,
                (upload_id,),
            ).fetchone()

        if not upload:
            abort(404)

        file_path = Path(upload["stored_path"])

        if not file_path.exists():
            abort(404)

        return send_file(
            file_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=upload["stored_filename"],
        )


    @app.route("/uploads/<int:upload_id>/page/<int:page_number>")
    def view_upload_page(upload_id: int, page_number: int):
        # Most browsers/PDF viewers understand #page=N.
        return redirect(
            url_for("view_upload", upload_id=upload_id) + f"#page={page_number}"
        )

    @app.route("/search-filters")
    def search_filters():
        with get_connection(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT
                    sf.id,
                    sf.name,
                    sf.category,
                    sf.is_active,
                    sf.created_at,
                    sf.updated_at,
                    GROUP_CONCAT(st.term, ', ') AS terms,
                    COUNT(st.id) AS term_count
                FROM search_filters sf
                LEFT JOIN search_terms st ON st.filter_id = sf.id
                GROUP BY sf.id
                ORDER BY sf.category, sf.name
                """
            ).fetchall()

        return render_template("search_filters.html", rows=rows)

    @app.route("/search-filters/new", methods=["GET", "POST"])
    def create_search_filter():
        errors = []
        name = ""
        category = ""
        terms_text = ""

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            terms_text = request.form.get("terms", "").strip()

            terms = [
                term.strip()
                for term in re.split(r"[\n,]+", terms_text)
                if term.strip()
            ]

            if not name:
                errors.append("Filter name is required.")

            if not category:
                errors.append("Category is required.")

            if not terms:
                errors.append("At least one search term is required.")

            with get_connection(DB_PATH) as conn:
                existing = conn.execute(
                    """
                    SELECT id
                    FROM search_filters
                    WHERE LOWER(name) = LOWER(?)
                    """,
                    (name,),
                ).fetchone()

                if existing:
                    errors.append("A search filter with this name already exists.")

                if not errors:
                    cur = conn.execute(
                        """
                        INSERT INTO search_filters (name, category, is_active)
                        VALUES (?, ?, 1)
                        """,
                        (name, category),
                    )
                    filter_id = cur.lastrowid

                    seen_terms = set()

                    for term in terms:
                        key = term.lower()

                        if key in seen_terms:
                            continue

                        seen_terms.add(key)

                        conn.execute(
                            """
                            INSERT INTO search_terms (filter_id, term)
                            VALUES (?, ?)
                            """,
                            (filter_id, term),
                        )

                    conn.commit()

                    return redirect(url_for("search_filters"))

        return render_template(
            "search_filter_form.html",
            mode="create",
            title="Create Search Filter",
            action_url=url_for("create_search_filter"),
            name=name,
            category=category,
            terms_text=terms_text,
            errors=errors,
        )

    @app.route("/search-filters/<int:filter_id>/edit", methods=["GET", "POST"])
    def edit_search_filter(filter_id: int):
        errors = []

        with get_connection(DB_PATH) as conn:
            search_filter = conn.execute(
                """
                SELECT id, name, category, is_active
                FROM search_filters
                WHERE id = ?
                """,
                (filter_id,),
            ).fetchone()

            if not search_filter:
                abort(404)

            term_rows = conn.execute(
                """
                SELECT term
                FROM search_terms
                WHERE filter_id = ?
                ORDER BY term
                """,
                (filter_id,),
            ).fetchall()

            if request.method == "GET":
                name = search_filter["name"]
                category = search_filter["category"]
                terms_text = "\n".join(row["term"] for row in term_rows)
                is_active = bool(search_filter["is_active"])

            else:
                name = request.form.get("name", "").strip()
                category = request.form.get("category", "").strip()
                terms_text = request.form.get("terms", "").strip()
                is_active = request.form.get("is_active") == "1"

                terms = [
                    term.strip()
                    for term in re.split(r"[\n,]+", terms_text)
                    if term.strip()
                ]

                if not name:
                    errors.append("Filter name is required.")

                if not category:
                    errors.append("Category is required.")

                if not terms:
                    errors.append("At least one search term is required.")

                duplicate = conn.execute(
                    """
                    SELECT id
                    FROM search_filters
                    WHERE LOWER(name) = LOWER(?)
                    AND id != ?
                    """,
                    (name, filter_id),
                ).fetchone()

                if duplicate:
                    errors.append("A search filter with this name already exists.")

                if not errors:
                    conn.execute(
                        """
                        UPDATE search_filters
                        SET name = ?,
                            category = ?,
                            is_active = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (name, category, 1 if is_active else 0, filter_id),
                    )

                    conn.execute(
                        """
                        DELETE FROM search_terms
                        WHERE filter_id = ?
                        """,
                        (filter_id,),
                    )

                    seen_terms = set()

                    for term in terms:
                        key = term.lower()

                        if key in seen_terms:
                            continue

                        seen_terms.add(key)

                        conn.execute(
                            """
                            INSERT INTO search_terms (filter_id, term)
                            VALUES (?, ?)
                            """,
                            (filter_id, term),
                        )

                    conn.commit()

                    return redirect(url_for("search_filters"))

        return render_template(
            "search_filter_form.html",
            mode="edit",
            title="Edit Search Filter",
            action_url=url_for("edit_search_filter", filter_id=filter_id),
            name=name,
            category=category,
            terms_text=terms_text,
            is_active=is_active,
            errors=errors,
        )

    @app.route("/search-filters/<int:filter_id>/delete", methods=["POST"])
    def delete_search_filter(filter_id: int):
        with get_connection(DB_PATH) as conn:
            search_filter = conn.execute(
                """
                SELECT id
                FROM search_filters
                WHERE id = ?
                """,
                (filter_id,),
            ).fetchone()

            if not search_filter:
                abort(404)

            conn.execute(
                """
                DELETE FROM search_filters
                WHERE id = ?
                """,
                (filter_id,),
            )
            conn.commit()

        return redirect(url_for("search_filters"))

    @app.route("/projects/<int:project_id>/term-highlight")
    def view_project_term_highlight(project_id: int):
        term = request.args.get("term", "").strip()

        if not term:
            abort(400, "Search term is required")

        with get_connection(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT
                    u.id AS upload_id,
                    u.stored_filename,
                    u.stored_path,
                    sr.page_number,
                    st.term AS search_term
                FROM search_results sr
                JOIN uploads u ON u.id = sr.upload_id
                JOIN search_terms st ON st.id = sr.term_id
                WHERE sr.project_id = ?
                  AND lower(st.term) = lower(?)
                ORDER BY u.stored_filename, sr.page_number
                """,
                (project_id, term),
            ).fetchall()

        if not rows:
            abort(404)

        documents = []
        documents_by_upload = {}
        highlight_term = rows[0]["search_term"]

        for row in rows:
            file_path = Path(row["stored_path"])

            if not file_path.exists():
                continue

            upload_id = row["upload_id"]

            if upload_id not in documents_by_upload:
                documents_by_upload[upload_id] = {
                    "upload": {
                        "id": upload_id,
                        "stored_filename": row["stored_filename"],
                    },
                    "pdf_url": url_for("view_upload", upload_id=upload_id),
                    "page_numbers": [],
                    "_seen_pages": set(),
                }
                documents.append(documents_by_upload[upload_id])

            document = documents_by_upload[upload_id]
            page_number = row["page_number"]

            if page_number not in document["_seen_pages"]:
                document["page_numbers"].append(page_number)
                document["_seen_pages"].add(page_number)

        for document in documents:
            document.pop("_seen_pages", None)

        if not documents:
            abort(404)

        first_document = documents[0]
        first_upload = first_document["upload"]
        first_page_number = first_document["page_numbers"][0]

        return render_template(
            "pdf_viewer.html",
            upload=first_upload,
            page_number=first_page_number,
            page_numbers=first_document["page_numbers"],
            documents=documents,
            term=highlight_term,
            result=None,
            pdf_url=first_document["pdf_url"],
        )

    @app.route("/uploads/<int:upload_id>/page/<int:page_number>/highlight")
    def view_upload_page_highlight(upload_id: int, page_number: int):
        term = request.args.get("term", "").strip()
        result_id = request.args.get("result_id", "").strip()
        pages_arg = request.args.get("pages", "").strip()

        with get_connection(DB_PATH) as conn:
            upload = conn.execute(
                """
                SELECT id, stored_filename, stored_path
                FROM uploads
                WHERE id = ?
                """,
                (upload_id,),
            ).fetchone()

            if not upload:
                abort(404)

            result = None
            if result_id.isdigit():
                result = conn.execute(
                    """
                    SELECT
                        sr.id,
                        sr.matched_text,
                        sr.context_text,
                        st.term AS search_term
                    FROM search_results sr
                    JOIN search_terms st ON st.id = sr.term_id
                    WHERE sr.id = ?
                    """,
                    (int(result_id),),
                ).fetchone()

        file_path = Path(upload["stored_path"])

        if not file_path.exists():
            abort(404)

        highlight_term = term

        if result and result["search_term"]:
            highlight_term = result["search_term"]

        page_numbers = []
        for value in pages_arg.split(","):
            value = value.strip()
            if value.isdigit():
                parsed_page = int(value)
                if parsed_page not in page_numbers:
                    page_numbers.append(parsed_page)

        if not page_numbers:
            page_numbers = [page_number]

        pdf_url = url_for("view_upload", upload_id=upload_id)
        documents = [
            {
                "upload": {
                    "id": upload["id"],
                    "stored_filename": upload["stored_filename"],
                },
                "pdf_url": pdf_url,
                "page_numbers": page_numbers,
            }
        ]

        return render_template(
            "pdf_viewer.html",
            upload=upload,
            page_number=page_number,
            page_numbers=page_numbers,
            documents=documents,
            term=highlight_term,
            result=result,
            pdf_url=pdf_url,
        )


    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)