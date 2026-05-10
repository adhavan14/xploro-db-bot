"""
MySQL → Markdown Exporter (Business-Scoped)
--------------------------------------------
Exports data for a SINGLE business across all related tables.
Handles both direct (business_id column) and indirect (via joins) relationships.

Install:
    pip install mysql-connector-python tabulate

Usage:
    python mysql_export_business.py --config export_config.json --business_id 42

Output:
    db_export.md  (ready to upload to Claude Files API)
"""

import json
import argparse
import mysql.connector
from tabulate import tabulate
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta


# ─────────────────────────────────────────
# LOAD CONFIG
# ─────────────────────────────────────────

def load_config(config_path):
    with open(config_path, "r") as f:
        return json.load(f)


# ─────────────────────────────────────────
# DB CONNECTION
# ─────────────────────────────────────────

def get_connection(db_cfg):
    return mysql.connector.connect(
        host=db_cfg["host"],
        port=db_cfg.get("port", 3306),
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
    )


# ─────────────────────────────────────────
# DATE FILTER HELPERS
# ─────────────────────────────────────────

def get_date_threshold(days: int) -> str:
    """Returns the cutoff date string for N days ago."""
    cutoff = datetime.now() - relativedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def get_table_columns_list(cursor, table: str) -> list[str]:
    """Returns list of column names for a table."""
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return [row[0] for row in cursor.fetchall()]


def resolve_date_column(cursor, table: str, date_filter_cfg: dict) -> str | None:
    """
    Finds which date column exists on this table.
    Tries 'column' first, then 'fallback_column', then None.
    """
    if not date_filter_cfg.get("enabled", False):
        return None

    cols = get_table_columns_list(cursor, table)
    primary   = date_filter_cfg.get("column", "CreatedAt")
    fallback  = date_filter_cfg.get("fallback_column", "ModifiedAt")

    if primary in cols:
        return primary
    if fallback in cols:
        return fallback

    return None  # table has neither column — skip date filter




def fetch_business(cursor, business_table_cfg, business_id):
    table  = business_table_cfg["table"]
    pk_col = business_table_cfg["pk"]

    cursor.execute(f"SELECT * FROM `{table}` WHERE `{pk_col}` = %s", (business_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"No business found with id={business_id} in table '{table}'")

    columns = [desc[0] for desc in cursor.description]
    return columns, row


# ─────────────────────────────────────────
# BUILD QUERY PER TABLE TYPE
# ─────────────────────────────────────────

def build_query(table_cfg, business_id, row_limit, date_col=None, date_threshold=None):
    table = table_cfg["table"]
    limit = f"LIMIT {row_limit}" if row_limit else ""

    # Date filter clause (applied on the main table)
    date_clause = ""
    if date_col and date_threshold:
        date_clause = f"AND `{table}`.`{date_col}` >= '{date_threshold}'"

    if table_cfg["link"] == "direct":
        biz_col = table_cfg["business_id_col"]
        query = f"""
            SELECT `{table}`.* 
            FROM `{table}`
            WHERE `{table}`.`{biz_col}` = %s
            {date_clause}
            ORDER BY `{table}`.`{date_col}` DESC
            {limit}
        """ if date_col else f"""
            SELECT `{table}`.* 
            FROM `{table}`
            WHERE `{table}`.`{biz_col}` = %s
            {limit}
        """
        return query, (business_id,)

    elif table_cfg["link"] == "indirect":
        via_table  = table_cfg["via_table"]
        via_join   = table_cfg["via_join"]
        biz_col    = table_cfg["via_business_id_col"]

        query = f"""
            SELECT `{table}`.* 
            FROM `{table}`
            INNER JOIN {via_table}
                ON {via_join}
            WHERE {biz_col} = %s
            {date_clause}
            ORDER BY `{table}`.`{date_col}` DESC
            {limit}
        """ if date_col else f"""
            SELECT `{table}`.* 
            FROM `{table}`
            INNER JOIN {via_table}
                ON {via_join}
            WHERE {biz_col} = %s
            {limit}
        """
        return query, (business_id,)

    else:
        raise ValueError(f"Unknown link type '{table_cfg['link']}' for table '{table}'")


# ─────────────────────────────────────────
# FETCH TABLE DATA
# ─────────────────────────────────────────

def fetch_table(cursor, table_cfg, business_id, row_limit, date_col=None, date_threshold=None):
    query, params = build_query(table_cfg, business_id, row_limit, date_col, date_threshold)
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return columns, rows, None
    except Exception as e:
        return [], [], str(e)


# ─────────────────────────────────────────
# FETCH FK RELATIONSHIPS (for context)
# ─────────────────────────────────────────

def get_foreign_keys(cursor, table, database):
    query = """
            SELECT
                COLUMN_NAME,
                REFERENCED_TABLE_NAME,
                REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL \
            """
    cursor.execute(query, (database, table))
    return cursor.fetchall()


# ─────────────────────────────────────────
# FORMAT HELPERS
# ─────────────────────────────────────────

def fmt(val):
    if val is None:
        return "NULL"
    if isinstance(val, (bytes, bytearray)):
        return "[binary]"
    return str(val)


def rows_to_markdown(columns, rows):
    if not rows:
        return "_No data found for this business._"
    formatted = [[fmt(v) for v in row] for row in rows]
    return tabulate(formatted, headers=columns, tablefmt="pipe")


# ─────────────────────────────────────────
# BUILD MARKDOWN SECTIONS
# ─────────────────────────────────────────

def build_header(db_name, business_id, biz_columns, biz_row, date_filter_cfg):
    biz_info = dict(zip(biz_columns, [fmt(v) for v in biz_row]))
    biz_summary = " | ".join([f"**{k}:** {v}" for k, v in biz_info.items()])

    date_note = ""
    if date_filter_cfg.get("enabled"):
        days = date_filter_cfg.get("days", 3)
        cutoff = datetime.now() - relativedelta(days=days)
        date_note = (
            f"\nDate Range  : Last {days} days "
            f"({cutoff.strftime('%Y-%m-%d')} to {datetime.now().strftime('%Y-%m-%d')})"
        )

    return f"""# Business Data Export — {db_name}
Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{date_note}
Business ID : {business_id}

> **Business Info:** {biz_summary}

---
"""


def build_schema_section(config, cursor):
    lines = ["## Schema & Relationships", ""]
    database = config["db"]["database"]
    all_tables = [t["table"] for t in config["tables"]]

    for t_cfg in config["tables"]:
        table = t_cfg["table"]
        link  = t_cfg["link"]

        # FK info
        fks = get_foreign_keys(cursor, table, database)
        fk_str = ""
        if fks:
            fk_str = " | FK: " + ", ".join(
                [f"`{col}` → `{ref_t}.{ref_c}`" for col, ref_t, ref_c in fks]
            )

        # Link description
        if link == "direct":
            link_str = f"direct via `{t_cfg['business_id_col']}`"
        else:
            link_str = f"indirect via `{t_cfg['via_table']}`"

        lines.append(f"- **{table}** — linked to business: {link_str}{fk_str}")

    lines.append("")
    return "\n".join(lines)


def build_table_section(table_cfg, columns, rows, error, row_limit, date_col=None, date_threshold=None):
    table = table_cfg["table"]
    link  = table_cfg["link"]
    lines = [f"## {table}"]

    # Link note
    if link == "direct":
        lines.append(f"*Filtered by: `{table_cfg['business_id_col']}`*")
    else:
        lines.append(f"*Filtered indirectly via: `{table_cfg['via_table']}`*")

    # Date filter note
    if date_col and date_threshold:
        lines.append(f"*Date filter: `{date_col}` >= `{date_threshold}` (last 3 days) — ordered by newest first*")
    elif date_col is None and table_cfg.get("link"):
        lines.append(f"*No date column found — full data exported*")

    lines.append("")

    if error:
        lines.append(f"⚠️ Error fetching data: `{error}`")
    else:
        lines.append(rows_to_markdown(columns, rows))
        if row_limit and len(rows) == row_limit:
            lines.append(f"\n> ⚠️ Showing first {row_limit} rows only (row limit reached).")

    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def export(config_path, business_id):
    config          = load_config(config_path)
    db_cfg          = config["db"]
    biz_cfg         = config["business_table"]
    tables_cfg      = config["tables"]
    row_limit       = config.get("row_limit", 500)
    output          = config.get("output_file", "db_export.md")
    date_filter_cfg = config.get("date_filter", {"enabled": False})

    # Compute date threshold once
    date_threshold = None
    if date_filter_cfg.get("enabled"):
        days = date_filter_cfg.get("days", 3)
        date_threshold = get_date_threshold(days)
        print(f"Date filter  : last {days} days (>= {date_threshold})")

    print(f"Connecting to {db_cfg['host']}/{db_cfg['database']}...")
    conn   = get_connection(db_cfg)
    cursor = conn.cursor()

    # ── Fetch business info ──
    print(f"Fetching business id={business_id}...")
    biz_columns, biz_row = fetch_business(cursor, biz_cfg, business_id)

    # ── Build markdown ──
    sections = []

    # Header
    sections.append(build_header(db_cfg["database"], business_id, biz_columns, biz_row, date_filter_cfg))

    # Schema overview
    sections.append(build_schema_section(config, cursor))
    sections.append("---\n")
    sections.append("## Table Data\n")

    # Each table
    for t_cfg in tables_cfg:
        table = t_cfg["table"]
        print(f"  Exporting: {table}...")

        # Resolve which date column this table has
        date_col = resolve_date_column(cursor, table, date_filter_cfg)
        if date_filter_cfg.get("enabled"):
            if date_col:
                print(f"    Date col : {date_col}")
            else:
                print(f"    Date col : not found — exporting all rows")

        columns, rows, error = fetch_table(
            cursor, t_cfg, business_id, row_limit,
            date_col=date_col,
            date_threshold=date_threshold,
        )

        if error:
            print(f"    ⚠️  Error: {error}")
        else:
            print(f"    ✓  {len(rows)} rows")

        section = build_table_section(
            t_cfg, columns, rows, error, row_limit,
            date_col=date_col,
            date_threshold=date_threshold,
        )
        sections.append(section)
        sections.append("---\n")

    # Write file
    content = "\n".join(sections)
    with open(output, "w", encoding="utf-8") as f:
        f.write(content)

    cursor.close()
    conn.close()

    size_kb = len(content.encode("utf-8")) / 1024
    print(f"\n✅ Export complete!")
    print(f"   File       : {output}")
    print(f"   Size       : {size_kb:.1f} KB")
    print(f"   Business ID: {business_id}")
    print(f"   Tables     : {len(tables_cfg)}")
    print(f"\nNext: Upload '{output}' to Claude Files API.")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export MySQL data for a specific business")
    parser.add_argument("--config",      required=True,  help="Path to export_config.json")
    parser.add_argument("--business_id", required=True,  help="Business ID to filter by")
    args = parser.parse_args()

    export(args.config, args.business_id)