"""
ClinicalTrialsTool — query ClinicalTrials.gov API v2 for trial data.

Spec: docs/CREWAI_TOOLS.md v2.0, Section 5
Used by: Agents 001, 002, 003, 004, 008, 010

API v2 note: CT.gov replaced API v1 in 2024. All field paths use nested JSON:
  protocolSection.statusModule.overallStatus (not flat 'status')
"""

import json

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

CT_BASE = "https://clinicaltrials.gov/api/v2/studies"


class ClinicalTrialsQueryInput(BaseModel):
    query: str = Field(description="Search terms for trial title, condition, or drug name")
    sponsor: str = Field(
        default=None,
        description="Sponsor name filter (searches lead sponsor and collaborators)",
    )
    status: str = Field(
        default=None,
        description="Trial status filter: RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED, TERMINATED",
    )
    phase: str = Field(
        default=None,
        description="Phase filter: PHASE1, PHASE2, PHASE3",
    )
    max_results: int = Field(
        default=20,
        description="Maximum results to return (max 100)",
    )


class ClinicalTrialsTool(BaseTool):
    name: str = "clinicaltrials_search"
    description: str = (
        "Search ClinicalTrials.gov API v2 for clinical trials. "
        "Use to find: trial status for a company, competitor trials in an indication, "
        "ACTIVE_NOT_RECRUITING Phase 3 trials (NDA candidates), terminated trials. "
        "Returns NCT ID, title, phase, status, sponsor, completion dates."
    )
    args_schema: type[BaseModel] = ClinicalTrialsQueryInput

    def _run(
        self,
        query: str,
        sponsor: str = None,
        status: str = None,
        phase: str = None,
        max_results: int = 20,
    ) -> str:
        try:
            params: dict = {
                "query.term": query,
                "pageSize": min(max_results, 100),
                "format": "json",
            }
            if sponsor:
                params["query.spons"] = sponsor
            if status:
                params["filter.overallStatus"] = status
            if phase:
                params["filter.advanced"] = f"AREA[Phase]{phase}"

            resp = requests.get(CT_BASE, params=params, timeout=20)
            resp.raise_for_status()

            studies = resp.json().get("studies", [])
            results = []
            for s in studies:
                ps = s.get("protocolSection", {})
                id_mod = ps.get("identificationModule", {})
                status_mod = ps.get("statusModule", {})
                design_mod = ps.get("designModule", {})
                sponsor_mod = ps.get("sponsorCollaboratorsModule", {})

                results.append({
                    "nct_id": id_mod.get("nctId"),
                    "title": id_mod.get("briefTitle"),
                    "phase": design_mod.get("phases", []),
                    "status": status_mod.get("overallStatus"),
                    "sponsor": sponsor_mod.get("leadSponsor", {}).get("name"),
                    "sponsor_class": sponsor_mod.get("leadSponsor", {}).get("class"),
                    "completion_date": status_mod.get("primaryCompletionDateStruct", {}).get("date"),
                    "enrollment": design_mod.get("enrollmentInfo", {}).get("count"),
                })

            return json.dumps(results)
        except requests.exceptions.Timeout:
            return "ERROR: Request timed out. Try again or use a cached result."
        except requests.exceptions.HTTPError as e:
            return f"ERROR: HTTP {e.response.status_code} — {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
