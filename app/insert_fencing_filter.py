from app.db import get_connection

with get_connection("data/cqe.db") as conn:
    cur = conn.execute(
        """
        INSERT INTO search_filters (name, category, is_active)
        VALUES (?, ?, 1)
        """,
        ("Fabral", "All"),
    )
    filter_id = cur.lastrowid

    terms = [
        "Cornerstone", 
        "Drexel", 
        "Stand 'N Seam", 
        "Stand N Seam", 
        "Standing Seam", 
        "R-Panel", 
        "Snap-Lock", 
        "Snap Lock", 
        "Metal Sales", 
        "Fabral", 
        "Centria", 
        "McElroy", 
        "Central States", 
        "PVR Panel", 
        "ATAS", 
        "Pac-Clad", 
        "SSMR", 
        "Angler", 
        "Pac Clad", 
        "Peterson"
    ]

    for term in terms:
        conn.execute(
            """
            INSERT INTO search_terms (filter_id, term)
            VALUES (?, ?)
            """,
            (filter_id, term),
        )

    conn.commit()

print("Inserted filter:", filter_id)