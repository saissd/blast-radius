#!/usr/bin/env python3
"""
risk_score.py
Usage:
    python risk_score.py [--json] [--gate] <name>

<name> is matched case-insensitively against:
  - program IDs    (e.g. SAM2)
  - DD file names  (e.g. CUSTFILE)
  - copybook names (e.g. CUSTCOPY)

Outputs a risk score (0-100), band (HIGH / MEDIUM / LOW), a plain-English
reason, and a regression test list (affected programs that need retesting).

Scoring weights:
  - Each affected program  x3
  - Each file written      x5
  - Each shared copybook   x4
  - Each JCL job affected  x6
  Score is capped at 100.

Bands:
  HIGH   >= 60
  MEDIUM 30-59
  LOW     < 30

Flags:
  --json   Output structured JSON.
  --gate   Exit with code 1 if the band is HIGH, 0 otherwise.
           Combine with --json to get structured output AND a gate exit code.
"""

import argparse
import json
import sys
from pathlib import Path

IMPACT_MAP   = Path(__file__).parent / "impact_map.json"
DEPENDENCIES = Path(__file__).parent / "dependencies.json"

WEIGHT_PROGRAM   = 3
WEIGHT_FILE      = 5
WEIGHT_COPYBOOK  = 4
WEIGHT_JCL_JOB   = 6


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

def _load(path: Path, name: str) -> dict:
    if not path.exists():
        sys.exit(f"Error: {path} not found.")
    with path.open() as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Impact-map search  (mirrors blast_radius.py search())
# --------------------------------------------------------------------------- #

def _search(impact_map: dict, name: str) -> list[tuple[str, str, list[dict]]]:
    """Return (category, matched_key, depended_on_by) for every category match."""
    name_upper = name.upper()
    hits = []
    for category in ("programs", "files", "copybooks"):
        for key, value in impact_map.get(category, {}).items():
            if key.upper() == name_upper:
                hits.append((category, key, value["depended_on_by"]))
    return hits


# --------------------------------------------------------------------------- #
# Core scoring
# --------------------------------------------------------------------------- #

def compute_risk(name: str, impact_map: dict, deps_data: dict) -> dict:
    hits = _search(impact_map, name)

    if not hits:
        # Entity not found in impact map
        return {
            "entity": name.upper(),
            "score": 0,
            "band": "LOW",
            "reason": f"'{name.upper()}' was not found in impact_map.json. "
                      "No affected programs, files, or copybooks could be identified.",
            "regression_tests": [],
        }

    # ------------------------------------------------------------------ #
    # Collect unique affected programs across all category hits
    # ------------------------------------------------------------------ #
    affected_program_ids: set[str] = set()
    output_files: set[str] = set()          # files written (OUTPUT access)
    shared_copybooks: set[str] = set()
    entity_categories: list[str] = []

    for category, key, dependents in hits:
        entity_categories.append(category)
        for dep in dependents:
            pid = dep.get("program_id", "")
            if pid:
                affected_program_ids.add(pid)

    # From dependencies.json: collect OUTPUT files and copybook sharing
    # OUTPUT files: any file with access==OUTPUT touched by affected programs
    # Shared copybooks: copybooks used by more than one distinct program_id
    copybook_users: dict[str, set[str]] = {}   # copybook_name -> {program_ids}

    for prog in deps_data.get("programs", []):
        pid = prog.get("program_id", "")
        if pid not in affected_program_ids:
            continue
        for f in prog.get("files", []):
            if f.get("access", "").upper() == "OUTPUT":
                output_files.add(f["dd_name"])
        for cb in prog.get("copybooks", []):
            cb_name = cb["name"]
            copybook_users.setdefault(cb_name, set()).add(pid)

    # A copybook counts as "shared" when more than one distinct program uses it
    for cb_name, users in copybook_users.items():
        if len(users) > 1:
            shared_copybooks.add(cb_name)

    # ------------------------------------------------------------------ #
    # JCL jobs affected: unique job names that execute any affected program
    # ------------------------------------------------------------------ #
    executed_by = impact_map.get("jcl", {}).get("programs_executed_by", {})
    affected_jobs: set[str] = set()
    for pid in affected_program_ids:
        for step_entry in executed_by.get(pid, []):
            job = step_entry.get("job", "")
            if job:
                affected_jobs.add(job)

    # ------------------------------------------------------------------ #
    # Raw score
    # ------------------------------------------------------------------ #
    n_programs  = len(affected_program_ids)
    n_files     = len(output_files)
    n_copybooks = len(shared_copybooks)
    n_jobs      = len(affected_jobs)

    raw = (
        n_programs  * WEIGHT_PROGRAM  +
        n_files     * WEIGHT_FILE     +
        n_copybooks * WEIGHT_COPYBOOK +
        n_jobs      * WEIGHT_JCL_JOB
    )
    score = min(raw, 100)

    # ------------------------------------------------------------------ #
    # Band
    # ------------------------------------------------------------------ #
    if score >= 60:
        band = "HIGH"
    elif score >= 30:
        band = "MEDIUM"
    else:
        band = "LOW"

    # ------------------------------------------------------------------ #
    # Plain-English reason
    # ------------------------------------------------------------------ #
    category_label = {
        "programs":  "program",
        "files":     "DD file",
        "copybooks": "copybook",
    }
    entity_type = category_label.get(entity_categories[0], "entity")
    if len(set(entity_categories)) > 1:
        entity_type = "entity"  # appears in multiple categories

    parts = []
    if n_programs:
        parts.append(
            f"{n_programs} affected program{'s' if n_programs != 1 else ''} "
            f"({', '.join(sorted(affected_program_ids))})"
        )
    if n_files:
        parts.append(
            f"{n_files} output file{'s' if n_files != 1 else ''} written "
            f"({', '.join(sorted(output_files))})"
        )
    if n_copybooks:
        parts.append(
            f"{n_copybooks} shared copybook{'s' if n_copybooks != 1 else ''} "
            f"({', '.join(sorted(shared_copybooks))})"
        )
    if n_jobs:
        parts.append(
            f"{n_jobs} JCL job{'s' if n_jobs != 1 else ''} affected "
            f"({', '.join(sorted(affected_jobs))})"
        )

    if parts:
        reason = (
            f"Changing the {entity_type} '{name.upper()}' impacts: "
            + "; ".join(parts)
            + f". Raw weighted score {raw}"
            + (f" capped to {score}" if raw > 100 else "")
            + "."
        )
    else:
        reason = (
            f"'{name.upper()}' is present in the impact map but has no "
            "downstream dependents, output files, shared copybooks, or "
            "JCL jobs. Change is low risk."
        )

    regression_tests = sorted(affected_program_ids)

    return {
        "entity": name.upper(),
        "score": score,
        "band": band,
        "reason": reason,
        "regression_tests": regression_tests,
    }


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def print_text(result: dict) -> None:
    print(f"Entity  : {result['entity']}")
    print(f"Score   : {result['score']}/100")
    print(f"Band    : {result['band']}")
    print(f"Reason  : {result['reason']}")
    if result["regression_tests"]:
        print(f"Regression tests : {', '.join(result['regression_tests'])}")
    else:
        print("Regression tests : (none)")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute change-risk score for a COBOL program, file, or copybook."
    )
    parser.add_argument("name", help="Program ID, DD file name, or copybook name")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit with code 1 if band is HIGH, 0 otherwise (for CI use)",
    )
    args = parser.parse_args()

    impact_map = _load(IMPACT_MAP, "impact_map.json")
    deps_data  = _load(DEPENDENCIES, "dependencies.json")

    result = compute_risk(args.name, impact_map, deps_data)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_text(result)

    if args.gate and result["band"] == "HIGH":
        sys.exit(1)


if __name__ == "__main__":
    main()
