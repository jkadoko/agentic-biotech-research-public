"""
AACT bulk CSV ingestion — nightly 02:00 via APScheduler.

Downloads 6 pipe-delimited CSV files from AACT (no PostgreSQL dependency):
  studies | sponsors | conditions | interventions | design_outcomes | collaborators

AACT bulk export URL (publicly accessible, no API key):
  https://aact.ctti-clinicaltrials.org/pipe_files

REQ-034: Only INDUSTRY-class sponsors are stored; NIH/FED/NETWORK/OTHER are discarded.
REQ-074: enrollment_is_actual is derived at upsert time from enrollment_type.
REQ-079: Row-count anomaly check — alert if studies count drops > 10%.

System metadata keys written:
  aact_last_sync           — ISO timestamp of successful upsert
  aact_studies_prev_count  — count of studies rows from this run
"""

import csv
import io
import logging
import os
import zipfile
from datetime import datetime

import requests

from src.db.data_manager import (
    get_metadata,
    get_session,
    set_metadata,
    upsert_collaborator,
    upsert_condition,
    upsert_design_outcome,
    upsert_intervention,
    upsert_study,
)
from src.db.models import init_db

log = logging.getLogger(__name__)

AACT_BASE_URL = "https://aact.ctti-clinicaltrials.org/pipe_files"
# The nightly static export is a zip file containing all tables
AACT_ZIP_URL = f"{AACT_BASE_URL}/daily/latest.zip"

# Tables we want from the zip (filenames inside the archive)
WANTED_FILES = {
    "studies.txt",
    "sponsors.txt",
    "conditions.txt",
    "interventions.txt",
    "design_outcomes.txt",
    "collaborators.txt",
}


# ---------------------------------------------------------------------------
# Download + parse
# ---------------------------------------------------------------------------


def _download_zip(url: str, timeout: int = 120) -> bytes:
    log.info("Downloading AACT export from %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _parse_pipe_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content), delimiter="|")
    return [row for row in reader]


def _extract_tables(zip_bytes: bytes) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            basename = os.path.basename(name)
            if basename in WANTED_FILES:
                with zf.open(name) as f:
                    content = f.read().decode("utf-8", errors="replace")
                    tables[basename] = _parse_pipe_csv(content)
                    log.info("AACT: loaded %s — %d rows", basename, len(tables[basename]))
    return tables


# ---------------------------------------------------------------------------
# Upsert routines per table
# ---------------------------------------------------------------------------


def _upsert_studies(session, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        nct_id = row.get("nct_id") or row.get("NCT_ID") or ""
        if not nct_id:
            continue
        data = {
            "nct_id": nct_id,
            "title": row.get("official_title") or row.get("brief_title"),
            "phase": row.get("phase"),
            "status": row.get("overall_status"),
            "study_type": row.get("study_type"),
            "start_date": _parse_date(row.get("start_date")),
            "primary_completion_date": _parse_date(row.get("primary_completion_date")),
            "enrollment": _int(row.get("enrollment")),
            "enrollment_type": row.get("enrollment_type"),
        }
        upsert_study(session, data)
        count += 1
    return count


def _upsert_sponsors(session, rows: list[dict]) -> None:
    """
    REQ-034: denormalize LEAD sponsors into studies.lead_sponsor / lead_sponsor_class.
    Collaborator rows are intentionally skipped here — all collaborators
    are handled exclusively by _upsert_collaborators() from collaborators.txt
    (the authoritative AACT collaborators table) to avoid duplicate writes.
    Non-industry lead sponsors are kept for lead_sponsor_class context.
    """
    from src.db.models import Study

    for row in rows:
        nct_id = row.get("nct_id") or ""
        role = (row.get("lead_or_collaborator") or "").lower()
        agency_class = row.get("agency_class") or ""
        name = row.get("name") or ""

        if role == "lead":
            study = session.get(Study, nct_id)
            if study:
                study.lead_sponsor = name or study.lead_sponsor
                study.lead_sponsor_class = agency_class or study.lead_sponsor_class
                session.add(study)
        # Collaborator rows skipped here — processed in _upsert_collaborators (REQ-034)


def _upsert_conditions(session, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        nct_id = row.get("nct_id") or ""
        name = row.get("name") or ""
        if not nct_id or not name:
            continue
        upsert_condition(session, {"nct_id": nct_id, "condition_name": name})
        count += 1
    return count


def _upsert_interventions(session, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        nct_id = row.get("nct_id") or ""
        name = row.get("name") or ""
        if not nct_id or not name:
            continue
        upsert_intervention(session, {
            "nct_id": nct_id,
            "drug_name": name,
            "intervention_type": row.get("intervention_type"),
        })
        count += 1
    return count


def _upsert_design_outcomes(session, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        nct_id = row.get("nct_id") or ""
        outcome_type = row.get("outcome_type") or ""
        measure = row.get("measure") or ""
        if not nct_id or not outcome_type or not measure:
            continue
        upsert_design_outcome(session, {
            "nct_id": nct_id,
            "outcome_type": outcome_type,
            "measure": measure,
        })
        count += 1
    return count


def _upsert_collaborators(session, rows: list[dict]) -> None:
    for row in rows:
        nct_id = row.get("nct_id") or ""
        name = row.get("name") or ""
        agency_class = row.get("agency_class") or ""
        if not nct_id or not name:
            continue
        # Only store INDUSTRY collaborators (REQ-034)
        if agency_class == "INDUSTRY":
            upsert_collaborator(session, {
                "nct_id": nct_id,
                "collaborator_name": name,
                "collaborator_class": agency_class,
            })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(val: str | None):
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            from datetime import datetime as dt
            return dt.strptime(val.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _int(val: str | None) -> int | None:
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# REQ-079: row count anomaly check
# ---------------------------------------------------------------------------


def _check_anomaly(session, new_count: int) -> None:
    prev_str = get_metadata(session, "aact_studies_prev_count")
    if prev_str:
        prev = int(prev_str)
        if prev > 0 and new_count < prev * 0.90:
            drop_pct = round((1 - new_count / prev) * 100, 1)
            log.error(
                "REQ-079 ANOMALY: studies count dropped %.1f%% (prev=%d, now=%d). "
                "Check AACT export integrity before next run.",
                drop_pct, prev, new_count,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> None:
    init_db()
    try:
        zip_bytes = _download_zip(AACT_ZIP_URL)
    except Exception as exc:
        log.error("Failed to download AACT export: %s", exc)
        return

    tables = _extract_tables(zip_bytes)
    if not tables:
        log.error("AACT zip contained no recognized table files")
        return

    with get_session() as session:
        studies_count = 0

        if "studies.txt" in tables:
            studies_count = _upsert_studies(session, tables["studies.txt"])
            log.info("Upserted %d studies", studies_count)

        if "sponsors.txt" in tables:
            _upsert_sponsors(session, tables["sponsors.txt"])

        if "conditions.txt" in tables:
            n = _upsert_conditions(session, tables["conditions.txt"])
            log.info("Upserted %d conditions", n)

        if "interventions.txt" in tables:
            n = _upsert_interventions(session, tables["interventions.txt"])
            log.info("Upserted %d interventions", n)

        if "design_outcomes.txt" in tables:
            n = _upsert_design_outcomes(session, tables["design_outcomes.txt"])
            log.info("Upserted %d design_outcomes", n)

        if "collaborators.txt" in tables:
            _upsert_collaborators(session, tables["collaborators.txt"])

        # REQ-079: check for anomalous row count drop
        _check_anomaly(session, studies_count)

        # Update system_metadata
        set_metadata(session, "aact_last_sync", datetime.utcnow().isoformat())
        set_metadata(session, "aact_studies_prev_count", str(studies_count))

        session.commit()
        log.info("fetch_aact_csvs: complete — %d studies upserted", studies_count)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
