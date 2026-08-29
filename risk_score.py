#!/usr/bin/env python3
"""
risk_score.py
Usage:
    python risk_score.py [--json] [--gate] [--report] <name>

<name> is matched case-insensitively against:
  - program IDs    (e.g. SAM2)
  - DD file names  (e.g. CUSTFILE)
  - copybook names (e.g. CUSTCOPY)

Outputs a risk score (0-100), band (HIGH / MEDIUM / LOW), a plain-English
reason, and a regression test list (affected programs that need retesting).

Scoring weights:
  - Each affected program           x5
  - Each OUTPUT file written        x3
  - Each copybook source-file use   x8   (total #files that COPY this copybook;
                                          or shared copybooks when scoring a
                                          program/file entity)
  - Each JCL job affected           x5
  - Category base score             +25 for programs, +0 for files/copybooks
  Score is capped at 100.

The category base ensures that a program depended on by others (call-chain risk)
scores above a raw DD file with an identical dependency footprint.

Root programs (entry-points that nothing calls, e.g. SAM1) are scored on what
they *depend on* rather than what depends on them.

Bands:
  HIGH   >= 60
  MEDIUM 30-59
  LOW     < 30

Flags:
  --json     Output structured JSON.
  --gate     Exit with code 1 if the band is HIGH, 0 otherwise.
             Combine with --json or --report to get formatted output AND a gate
             exit code.
  --report   Output a formatted Markdown block suitable for posting as a
             GitHub PR comment section.
"""

import argparse
import json
import sys
from pathlib import Path

IMPACT_MAP   = Path(__file__).parent / "impact_map.json"
DEPENDENCIES = Path(__file__).parent / "dependencies.json"

WEIGHT_PROGRAM   = 5
WEIGHT_FILE      = 3
WEIGHT_COPYBOOK  = 8
WEIGHT_JCL_JOB   = 5

# Per-category base score added once to every non-root entity score.
# Programs carry inherent call-chain risk that a bare DD file does not.
CATEGORY_BASE = {
    "programs":  25,
    "files":      0,
    "copybooks":  0,
}


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

def _is_root_program(
    hits: list[tuple[str, str, list[dict]]],
    name: str,
    deps_data: dict,
) -> bool:
    """
    Mirror of blast_radius.is_root_program().
    Returns True when <name> is a known program that nothing calls.
    Two cases:
      1. Absent from impact_map entirely but present in dependencies.json programs.
      2. Present in impact_map.programs with an empty depended_on_by and no other
         category hit that has callers.
    """
    name_upper = name.upper()
    if not hits:
        return any(
            p.get("program_id", "").upper() == name_upper
            for p in deps_data.get("programs", [])
        )
    program_hits     = [h for h in hits if h[0] == "programs"]
    non_program_hits = [h for h in hits if h[0] != "programs"]
    if not program_hits:
        return False
    all_empty             = all(len(deps) == 0 for _, _, deps in program_hits)
    any_non_prog_callers  = any(len(deps) > 0 for _, _, deps in non_program_hits)
    return all_empty and not any_non_prog_callers


def _root_program_facts(
    name: str,
    deps_data: dict,
    executed_by: dict,
) -> tuple[set[str], set[str], int, set[str]]:
    """
    For a root program: collect what *it depends on* from dependencies.json.
    Returns (called_programs, output_files, n_copybook_uses, affected_jobs).
    n_copybook_uses is the total count of distinct copybook names used (all are
    at risk when the entry-point changes, not just shared ones).
    """
    name_upper = name.upper()
    calls: set[str] = set()
    output_files: set[str] = set()
    copybooks_used: set[str] = set()

    for prog in deps_data.get("programs", []):
        if prog.get("program_id", "").upper() != name_upper:
            continue
        for c in prog.get("calls", []):
            if c.get("program"):
                calls.add(c["program"])
        for f in prog.get("files", []):
            if f.get("access", "").upper() == "OUTPUT":
                output_files.add(f["dd_name"])
        for cb in prog.get("copybooks", []):
            if cb.get("name"):
                copybooks_used.add(cb["name"])

    affected_jobs: set[str] = set()
    for step_entry in executed_by.get(name_upper, []):
        job = step_entry.get("job", "")
        if job:
            affected_jobs.add(job)

    return calls, output_files, len(copybooks_used), affected_jobs


def compute_risk(name: str, impact_map: dict, deps_data: dict) -> dict:
    hits = _search(impact_map, name)

    # ------------------------------------------------------------------ #
    # Root-program path
    # ------------------------------------------------------------------ #
    executed_by = impact_map.get("jcl", {}).get("programs_executed_by", {})

    if _is_root_program(hits, name, deps_data):
        calls, output_files, n_cb_uses, affected_jobs = _root_program_facts(
            name, deps_data, executed_by
        )
        n_programs  = len(calls)
        n_files     = len(output_files)
        n_copybooks = n_cb_uses
        n_jobs      = len(affected_jobs)
        raw = (
            n_programs  * WEIGHT_PROGRAM  +
            n_files     * WEIGHT_FILE     +
            n_copybooks * WEIGHT_COPYBOOK +
            n_jobs      * WEIGHT_JCL_JOB  +
            CATEGORY_BASE.get("programs", 0)
        )
        score = min(raw, 100)
        band  = "HIGH" if score >= 60 else ("MEDIUM" if score >= 30 else "LOW")

        parts = []
        if n_programs:
            parts.append(
                f"calls {n_programs} program{'s' if n_programs != 1 else ''} "
                f"({', '.join(sorted(calls))})"
            )
        if n_files:
            parts.append(
                f"writes {n_files} output file{'s' if n_files != 1 else ''} "
                f"({', '.join(sorted(output_files))})"
            )
        if n_copybooks:
            parts.append(f"uses {n_copybooks} copybook{'s' if n_copybooks != 1 else ''}")
        if n_jobs:
            parts.append(
                f"executed by {n_jobs} JCL job{'s' if n_jobs != 1 else ''} "
                f"({', '.join(sorted(affected_jobs))})"
            )

        reason = (
            f"'{name.upper()}' is an entry-point program (nothing calls it). "
            + (
                "It " + "; ".join(parts) + f". Raw weighted score {raw}"
                + (f" capped to {score}" if raw > 100 else "") + "."
                if parts else "No downstream dependencies found."
            )
        )
        regression_tests = sorted({name.upper()} | calls)
        return {
            "entity": name.upper(),
            "score": score,
            "band": band,
            "reason": reason,
            "regression_tests": regression_tests,
        }

    # ------------------------------------------------------------------ #
    # Unknown entity
    # ------------------------------------------------------------------ #
    if not hits:
        return {
            "entity": name.upper(),
            "score": 0,
            "band": "LOW",
            "reason": f"'{name.upper()}' was not found in impact_map.json. "
                      "No affected programs, files, or copybooks could be identified.",
            "regression_tests": [],
        }

    # ------------------------------------------------------------------ #
    # Normal path: entity has dependents in the impact map
    # ------------------------------------------------------------------ #
    affected_program_ids: set[str] = set()
    entity_categories: list[str] = []

    for category, key, dependents in hits:
        entity_categories.append(category)
        for dep in dependents:
            pid = dep.get("program_id", "")
            if pid:
                affected_program_ids.add(pid)

    output_files: set[str] = set()
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

    # ------------------------------------------------------------------ #
    # Copybook weight: use total source-file count from depended_on_by when
    # the entity itself is a copybook; use shared-copybook count otherwise.
    # ------------------------------------------------------------------ #
    cat0 = entity_categories[0]
    if cat0 == "copybooks":
        # Count total source files that COPY this copybook — every one of
        # those files must be recompiled, so breadth of use is the risk.
        n_copybooks = sum(len(dependents) for _, _, dependents in hits)
        cb_detail   = f"{n_copybooks} source file{'s' if n_copybooks != 1 else ''} COPY it"
    else:
        shared_copybooks: set[str] = {
            cb for cb, users in copybook_users.items() if len(users) > 1
        }
        n_copybooks = len(shared_copybooks)
        cb_detail   = (
            f"{n_copybooks} shared copybook{'s' if n_copybooks != 1 else ''} "
            f"({', '.join(sorted(shared_copybooks))})"
        )

    affected_jobs: set[str] = set()
    for pid in affected_program_ids:
        for step_entry in executed_by.get(pid, []):
            job = step_entry.get("job", "")
            if job:
                affected_jobs.add(job)

    n_programs  = len(affected_program_ids)
    n_files     = len(output_files)
    n_jobs      = len(affected_jobs)

    raw = (
        n_programs  * WEIGHT_PROGRAM  +
        n_files     * WEIGHT_FILE     +
        n_copybooks * WEIGHT_COPYBOOK +
        n_jobs      * WEIGHT_JCL_JOB  +
        CATEGORY_BASE.get(cat0, 0)
    )
    score = min(raw, 100)

    if score >= 60:
        band = "HIGH"
    elif score >= 30:
        band = "MEDIUM"
    else:
        band = "LOW"

    category_label = {
        "programs":  "program",
        "files":     "DD file",
        "copybooks": "copybook",
    }
    entity_type = category_label.get(cat0, "entity")
    if len(set(entity_categories)) > 1:
        entity_type = "entity"

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
        parts.append(cb_detail)
    if n_jobs:
        parts.append(
            f"{n_jobs} JCL job{'s' if n_jobs != 1 else ''} affected "
            f"({', '.join(sorted(affected_jobs))})"
        )

    base_score = CATEGORY_BASE.get(cat0, 0)
    if base_score:
        parts.append(f"+{base_score} program category base")

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


_BAND_BADGE = {
    "HIGH":   "🔴 HIGH",
    "MEDIUM": "🟡 MEDIUM",
    "LOW":    "🟢 LOW",
}


def format_markdown(result: dict) -> str:
    """Return a Markdown section suitable for a GitHub PR comment."""
    band_label = _BAND_BADGE.get(result["band"], result["band"])
    tests = result["regression_tests"]
    if tests:
        test_lines = "\n".join(f"- `{t}`" for t in tests)
    else:
        test_lines = "_None identified._"

    return (
        f"### `{result['entity']}` — {band_label}\n\n"
        f"**Score:** {result['score']}/100\n\n"
        f"**Reason:** {result['reason']}\n\n"
        f"**Regression tests:**\n\n"
        f"{test_lines}\n"
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute change-risk score for a COBOL program, file, or copybook."
    )
    parser.add_argument("name", help="Program ID, DD file name, or copybook name")
    parser.add_argument("--json",   action="store_true", help="Emit structured JSON")
    parser.add_argument("--report", action="store_true",
                        help="Emit formatted Markdown suitable for a GitHub PR comment")
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
    elif args.report:
        sys.stdout.buffer.write(format_markdown(result).encode("utf-8"))
    else:
        print_text(result)

    if args.gate and result["band"] == "HIGH":
        sys.exit(1)


if __name__ == "__main__":
    main()
