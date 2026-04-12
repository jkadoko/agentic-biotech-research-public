"""
Agent 004 — Peer Reviewer: Scientific Validation

Spec: docs/AGENT_004_PEER_REVIEWER.md v2.5
Crew: Analysis (Crew 2)
Model: llama3.1:8b (Ollama GPU0)

Audits clinical trial results for statistical rigor, endpoint integrity, and
clinical significance. Produces validity_score (0–100) and verdict.

Scoring:
  25 protocol integrity + 30 statistical validity + 20 clinical significance
  + 10 sample size - 25 red flags + 15 market size = 0–100

Verdicts: STRONG_SCIENCE (≥80) | SOLID (60–79) | WEAK (40–59) | VERY_WEAK (20–39)
          | FRAUD_RISK (<20 or active red flag)
"""

import os

from crewai import Agent, LLM, Task

from src.tools.clinicaltrials_tool import ClinicalTrialsTool
from src.tools.db_tool import DatabaseQueryTool, DatabaseWriteTool
from src.tools.local_rag_tool import LocalRAGTool
from src.tools.search_tool import DuckDuckGoSearchTool

_GPU0 = os.environ.get("OLLAMA_HOST_GPU0", "http://ollama-gpu0:11434")

_ENDPOINT_TAXONOMY = (
    "Oncology: OS > PFS/EFS/DFS > ORR > DCR. "
    "Hematology: MRD negativity (MM gold standard) > CR > DOR > TTR > EFS (DLBCL/AML). "
    "NASH: biopsy composite (fibrosis+NASH resolution, required by FDA) > MRI-PDFF > ALT. "
    "CV/HF: MACE-HF (CV death+HF hosp) > KCCQ (FDA-accepted co-primary for HFpEF) > 6MWD. "
    "CV/ASCVD: MACE-3 (CV death+MI+stroke). "
    "CV/AF: AF burden on Holter (rhythm) | stroke+systemic embolism (anticoagulation). "
    "CNS/AD: dual-pathway iADRS/CDR-SB (cognitive+functional required) > ADAS-Cog. "
    "CNS/MS: ARR (RRMS) | CDP (progressive) | NEDA-3 (commercial bar). "
    "CNS/PD: MDS-UPDRS Parts I–IV; neuroprotection requires delayed-start design. "
    "CNS/ALS: ALSFRS-R (≥20% slowing target) + OS pre-specified required."
)


def make_peer_reviewer_agent() -> Agent:
    llm = LLM(model="ollama/llama3.1:8b", base_url=_GPU0)
    return Agent(
        role="Clinical Trial Scientific Validator",
        goal=(
            "Audit clinical trial protocols and results for statistical integrity, "
            "endpoint hierarchy compliance, and clinical significance vs. standard of care. "
            "Detect endpoint switching, p-hacking, and underpowered studies. "
            "Assign a validity_score (0–100) and verdict that the Strategist can trust."
        ),
        backstory=(
            "You are a former academic oncologist turned drug development consultant who "
            "has reviewed 500+ Phase 2 and Phase 3 protocols. You know that OS is the "
            "only unambiguous endpoint in oncology, and that any trial switching from OS "
            "to PFS after interim analysis is a red flag. You've seen NASH trials fail "
            "because they used ALT instead of the FDA-required biopsy composite. "
            "You read protocol amendments and statistical analysis plans with suspicion, "
            "not optimism."
        ),
        tools=[
            DatabaseQueryTool(),
            DatabaseWriteTool(),
            LocalRAGTool(),
            DuckDuckGoSearchTool(),
            ClinicalTrialsTool(),
        ],
        llm=llm,
        verbose=False,
        max_iter=20,
    )


def make_peer_reviewer_task(agent: Agent, ticker: str, nct_id: str) -> Task:
    return Task(
        description=(
            f"SCIENTIFIC AUDIT — Validate clinical trial {nct_id} for {ticker}.\n\n"
            f"ENDPOINT TAXONOMY (apply strictly):\n{_ENDPOINT_TAXONOMY}\n\n"
            "TASK A — Protocol Integrity Check (0–25 pts):\n"
            "  STEP 1: Fetch current protocol from ClinicalTrialsTool (by nct_id).\n"
            "  STEP 2: Query historical endpoint data:\n"
            "    DatabaseQueryTool: SELECT outcome_type, measure "
            "    FROM design_outcomes WHERE nct_id = '{nct_id}' ORDER BY outcome_type.\n"
            "  STEP 3: Detect endpoint switching:\n"
            "    - Compare current primary_outcome vs. original registration.\n"
            "    - Switching primary endpoint: -15 pts, flag ENDPOINT_SWITCH.\n"
            "    - Upgrading secondary to primary after look: -20 pts, flag ENDPOINT_PROMOTION.\n"
            "  STEP 4: Verify primary endpoint is appropriate for indication:\n"
            "    - Oncology using ORR as primary without OS/PFS pre-specified: -10 pts.\n"
            "    - NASH using ALT only (no biopsy): -15 pts, INVALID_ENDPOINT.\n"
            "    - CNS/AD missing functional endpoint (no CDR-SB): -10 pts.\n"
            "  Baseline: 25 pts, subtract red flag penalties.\n\n"
            "TASK B — Statistical Validity (0–30 pts):\n"
            "  STEP 1: Search for trial results:\n"
            "    DuckDuckGoSearchTool: '{ticker} {nct_id} Phase 3 results p-value hazard ratio'\n"
            "  STEP 2: Score statistical quality:\n"
            "    +10: p-value < 0.001 with pre-specified primary endpoint\n"
            "    +8: p-value 0.001–0.01\n"
            "    +5: p-value 0.01–0.05\n"
            "    +0: p-value > 0.05 (not significant)\n"
            "    +10: HR < 0.70 for survival or ORR > 40% (strong effect size)\n"
            "    +7: HR 0.70–0.80 or ORR 25–40%\n"
            "    +5: HR 0.80–0.85 or ORR 15–25%\n"
            "    +0: HR > 0.85 or ORR < 15% (marginal effect)\n"
            "    +10: Multiplicity-controlled (Bonferroni/Hochberg) family of endpoints\n"
            "    +0: No multiplicity control\n\n"
            "TASK C — Clinical Significance vs. SoC (0–20 pts):\n"
            "  STEP 1: Identify current standard of care:\n"
            "    DuckDuckGoSearchTool: '{indication} current standard of care 2025 NCCN guidelines'\n"
            "  STEP 2: Compare effect size to SoC:\n"
            "    +20: Clearly superior (>30% improvement in primary endpoint)\n"
            "    +15: Moderately superior (15–30% improvement)\n"
            "    +8: Comparable to SoC with better safety\n"
            "    +3: Non-inferior with similar safety\n"
            "    +0: No clear advantage over SoC\n\n"
            "TASK D — Sample Size Adequacy (0–10 pts):\n"
            "  - DatabaseQueryTool: SELECT enrollment FROM studies WHERE nct_id = '{nct_id}'.\n"
            "  - 10 pts: enrollment ≥ 300 (well-powered Phase 3)\n"
            "  - 7 pts: 150–299\n"
            "  - 4 pts: 50–149 (underpowered risk)\n"
            "  - 0 pts: < 50 (likely underpowered)\n\n"
            "TASK E — Red Flag Language Scan (-0 to -25 pts):\n"
            "  DuckDuckGoSearchTool: '{ticker} clinical trial concerns FDA warning letter'\n"
            "  - p-value 0.049–0.051 with no pre-planned interim: -5 pts\n"
            "  - Post-hoc subgroup as headline result: -10 pts\n"
            "  - Missing confidence intervals in press release: -5 pts\n"
            "  - FDA complete response letter (CRL) history: -15 pts\n"
            "  - Active FDA warning letter: -25 pts, FRAUD_RISK trigger\n\n"
            "TASK F — Market Size Bonus (0–15 pts):\n"
            "  - DatabaseQueryTool: SELECT prevalence_us FROM disease_context "
            "    WHERE condition_normalized = '{indication}'.\n"
            "  - 15 pts: prevalence_us > 1,000,000 | 10 pts: 200,000–1,000,000 "
            "    | 5 pts: 50,000–200,000 (orphan) | 0 pts: < 50,000\n\n"
            "SCORING: validity_score = A + B + C + D - E_penalties + F (cap 0–100).\n"
            "VERDICT:\n"
            "  STRONG_SCIENCE: ≥80 | SOLID: 60–79 | WEAK: 40–59 | VERY_WEAK: 20–39\n"
            "  FRAUD_RISK: <20 OR active FDA warning letter OR ENDPOINT_SWITCH + p>0.05\n\n"
            "Write to agent_scientific_audits via DatabaseWriteTool:\n"
            "  ticker, nct_id, audit_date=today, validity_score, verdict, competitive_advantage,\n"
            "  endpoint_switching_detected (bool), red_flags (JSON array).\n\n"
            "Return JSON: {\"ticker\": str, \"nct_id\": str, \"validity_score\": int, "
            "\"verdict\": str, \"competitive_advantage\": str, "
            "\"endpoint_switching_detected\": bool, \"red_flags\": [str]}"
        ),
        expected_output=(
            "JSON with ticker, nct_id, validity_score (0–100), verdict "
            "(STRONG_SCIENCE|SOLID|WEAK|VERY_WEAK|FRAUD_RISK), competitive_advantage "
            "(SUPERIOR|FIRST_MOVER|COMPETITIVE|INFERIOR), endpoint_switching_detected (bool), "
            "and red_flags list."
        ),
        agent=agent,
    )
