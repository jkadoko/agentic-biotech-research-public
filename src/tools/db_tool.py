"""
DatabaseQueryTool, DatabaseWriteTool, DatabasePatchTool — shared CrewAI tools for SQLite.

Spec: docs/CREWAI_TOOLS.md v2.0, Sections 1, 2, 2b
REQ-004: DatabaseWriteTool never overwrites existing non-NULL values with NULL.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "biotech_tracker.db")

# Strict identifier validation to prevent SQL injection in dynamically built queries
IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z0-9_]+$")


# ---------------------------------------------------------------------------
# 1. DatabaseQueryTool
# ---------------------------------------------------------------------------


class DatabaseQueryInput(BaseModel):
    query: str = Field(
        description="SQL SELECT query to execute against biotech_tracker.db"
    )


class DatabaseQueryTool(BaseTool):
    name: str = "database_query"
    description: str = (
        "Execute a SQL SELECT query against the biotech database. "
        "Tables: companies, trial_pipeline, studies, conditions, catalysts, "
        "entity_aliases, disease_context, partnerships, agent_profiler_findings, "
        "agent_scientific_audits, agent_insider_findings, agent_volatility_findings, "
        "agent_smart_money_findings, smart_money_positions, agent_investment_memos, "
        "options_chains, orphan, news_articles, sec_filings, interventions, "
        "collaborators, design_outcomes, company_onboarding_log, historical_prices."
    )
    args_schema: type[BaseModel] = DatabaseQueryInput
    db_path: str = DB_PATH

    def _run(self, query: str) -> str:
        try:
            query_upper = query.strip().upper()
            if not (query_upper.startswith("SELECT") or query_upper.startswith("WITH") or query_upper.startswith("VALUES")):
                return "Error: Only SELECT, WITH, and VALUES queries are permitted."

            db_uri = f"{Path(self.db_path).absolute().as_uri()}?mode=ro"

            with sqlite3.connect(db_uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query)
                rows = [dict(row) for row in cursor.fetchall()]
                return json.dumps(rows[:100])  # cap at 100 rows to prevent context overflow
        except sqlite3.Error as e:
            return f"ERROR: sqlite3.Error: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 2. DatabaseWriteTool (REQ-004)
# ---------------------------------------------------------------------------


# Primary key map — must match SCHEMA.md exactly.
# Composite PKs expressed as a list. All values must be present in the data dict.
PRIMARY_KEY_MAP: dict[str, list[str]] = {
    # Core registry
    "companies":                  ["ticker"],
    "entity_aliases":             ["alias"],
    "disease_context":            ["condition_normalized"],
    # AACT-sourced trial tables
    "studies":                    ["nct_id"],
    "trial_pipeline":             ["ticker", "nct_id"],
    "conditions":                 ["nct_id", "condition_name"],
    "interventions":              ["nct_id", "drug_name"],
    "collaborators":              ["nct_id", "collaborator_name"],
    "design_outcomes":            ["nct_id", "outcome_type", "measure"],
    # Supporting tables
    "orphan":                     ["ticker", "drug_name"],
    "sec_filings":                ["ticker", "filing_date", "filing_type"],
    "company_onboarding_log":     ["ticker", "onboarding_date"],
    "news_articles":              ["url"],
    # Agent output tables
    "agent_profiler_findings":    ["ticker", "profile_date"],
    "agent_scientific_audits":    ["ticker", "nct_id", "audit_date"],
    "agent_insider_findings":     ["ticker", "scan_date"],
    "catalysts":                  ["ticker", "event_type", "event_date"],
    "agent_volatility_findings":  ["ticker", "scan_date"],
    "partnerships":               ["ticker", "partner_name", "drug_asset"],
    "agent_smart_money_findings": ["ticker", "scan_date"],
    "smart_money_positions":      ["ticker", "institution_name", "filing_date"],
    "agent_investment_memos":     ["ticker", "memo_date"],
}


class DatabaseWriteInput(BaseModel):
    table: str = Field(description="Target table name")
    data: dict = Field(
        description=(
            "Dictionary of column->value pairs to upsert. "
            "None/null values are ignored — will NOT overwrite existing non-NULL data (REQ-004)."
        )
    )


class DatabaseWriteTool(BaseTool):
    name: str = "database_write"
    description: str = (
        "REQ-004-compliant upsert: write agent findings to a database table. "
        "NULL/None values in data dict are NEVER written — existing non-NULL values are preserved. "
        "The primary key column(s) must be included in the data dict."
    )
    args_schema: type[BaseModel] = DatabaseWriteInput
    db_path: str = DB_PATH

    def _run(self, table: str, data: dict) -> str:
        try:
            if table not in PRIMARY_KEY_MAP:
                return f"Error: Table '{table}' is not in the allowed write list."

            pk_cols = PRIMARY_KEY_MAP[table]
            for pk in pk_cols:
                if pk not in data:
                    return f"Error: Primary key column '{pk}' must be present in data dict."

            # REQ-004: strip None values — never overwrite existing data with NULL
            clean_data = {k: v for k, v in data.items() if v is not None}
            if not clean_data:
                return "Warning: No non-null values to write."

            # Validate column names to prevent SQL injection
            for col in clean_data.keys():
                if not IDENTIFIER_REGEX.match(col):
                    return f"Error: Invalid column name '{col}'."

            cols = ", ".join(clean_data.keys())
            placeholders = ", ".join(["?"] * len(clean_data))
            conflict_target = ", ".join(pk_cols)

            non_pk_cols = [k for k in clean_data if k not in pk_cols]
            if non_pk_cols:
                set_clause = ", ".join(
                    [
                        f"{c} = CASE WHEN excluded.{c} IS NOT NULL THEN excluded.{c} ELSE {table}.{c} END"
                        for c in non_pk_cols
                    ]
                )
                sql = (
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                    f"ON CONFLICT({conflict_target}) DO UPDATE SET {set_clause}"
                )
            else:
                sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(sql, list(clean_data.values()))
                conn.commit()

            return (
                f"OK: Upserted 1 row into {table} "
                f"(pk={[data[p] for p in pk_cols]}, {len(non_pk_cols)} data columns written)"
            )
        except sqlite3.Error as e:
            return f"ERROR: sqlite3.Error: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 2b. DatabasePatchTool (Scout Task E — JSON column append/set)
# ---------------------------------------------------------------------------


PATCH_ALLOWED_TABLES = {"companies"}


class DatabasePatchInput(BaseModel):
    table: str = Field(description="Target table (currently only 'companies' is allowed)")
    pk_column: str = Field(description="Primary key column name (e.g., 'ticker')")
    pk_value: str = Field(description="Primary key value identifying the row to patch")
    json_column: str = Field(description="Name of the JSON column to patch (e.g., 'watchlist_flags')")
    operation: str = Field(
        description=(
            "JSON operation to perform. Options: "
            "'append' — appends value_dict as a new element to a JSON array; "
            "'set_key' — sets a top-level key in a JSON object to value_dict."
        )
    )
    value_dict: dict = Field(
        description="Dict to append (for 'append') or {key: value} to set (for 'set_key')"
    )


class DatabasePatchTool(BaseTool):
    name: str = "database_patch_json"
    description: str = (
        "Perform a targeted JSON column patch on a single row. "
        "Use to append a flag object to companies.watchlist_flags (Scout Task E M&A signals) "
        "without touching price, runway, or any other company column. REQ-004 safe."
    )
    args_schema: type[BaseModel] = DatabasePatchInput
    db_path: str = DB_PATH

    def _run(
        self,
        table: str,
        pk_column: str,
        pk_value: str,
        json_column: str,
        operation: str,
        value_dict: dict,
    ) -> str:
        try:
            if table not in PATCH_ALLOWED_TABLES:
                return f"Error: Table '{table}' is not in the patch-allowed list."
            if operation not in ("append", "set_key"):
                return "Error: operation must be 'append' or 'set_key'."

            if not IDENTIFIER_REGEX.match(pk_column):
                return f"Error: Invalid pk_column name '{pk_column}'."
            if not IDENTIFIER_REGEX.match(json_column):
                return f"Error: Invalid json_column name '{json_column}'."

            value_json = json.dumps(value_dict)
            with sqlite3.connect(self.db_path) as conn:
                if operation == "append":
                    sql = (
                        f"UPDATE {table} SET {json_column} = "
                        f"json_insert(COALESCE({json_column}, '[]'), '$[#]', json(?)) "
                        f"WHERE {pk_column} = ?"
                    )
                    conn.execute(sql, [value_json, pk_value])
                else:  # set_key
                    key = list(value_dict.keys())[0]
                    if not IDENTIFIER_REGEX.match(key):
                        return f"Error: Invalid JSON key '{key}'."
                    val = list(value_dict.values())[0]
                    val_json = json.dumps(val)
                    sql = (
                        f"UPDATE {table} SET {json_column} = "
                        f"json_set(COALESCE({json_column}, '{{}}'), '$.{key}', ?) "
                        f"WHERE {pk_column} = ?"
                    )
                    conn.execute(sql, [val_json, pk_value])
                conn.commit()

            return f"OK: Patched {table}.{json_column} for {pk_column}={pk_value} (op={operation})"
        except sqlite3.Error as e:
            return f"ERROR: sqlite3.Error: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
