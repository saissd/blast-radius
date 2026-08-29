#!/usr/bin/env python3
"""
blast_radius.py
Usage:
    python blast_radius.py [--json] <name>
    python blast_radius.py [--json] --changed <file1> [<file2> ...]
    python blast_radius.py [--json] --git-diff

<name> is matched case-insensitively against:
  - program IDs    (e.g. SAM2)
  - DD file names  (e.g. CUSTFILE)
  - copybook names (e.g. CUSTCOPY)

Prints every source file / program that would be affected if <name> changed,
grouped by category.  Also traces the JCL layer:
  - which JCL jobs/steps execute the affected programs
  - which datasets those steps produce or consume
  - which downstream jobs depend on those datasets

If the queried entity is a program that nothing calls (a root program),
the tool shows what it depends on (downstream dependencies) instead of
returning empty.

CONFIRMED and NEEDS_REVIEW edges are shown separately.
Uses impact_map.json (must be in the same directory).

Flags:
  --json        Output the full result as structured JSON instead of formatted text.
  --changed     Compute the combined blast radius across one or more changed files/names.
  --git-diff    Read changed files automatically from `git diff --name-only HEAD~1`
                and compute their combined blast radius.
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

IMPACT_MAP    = Path(__file__).parent / "impact_map.json"
DEPENDENCIES  = Path(__file__).parent / "dependencies.json"

CATEGORY_LABELS = {
    "programs":  "Programs that CALL",
    "files":     "Programs that open file (DD)",
    "copybooks": "Programs that COPY",
}

CONFIDENCE_ORDER = ["CONFIRMED", "NEEDS_REVIEW"]

# File extensions that are meaningful to blast-radius analysis.
# Anything outside this set is silently ignored in diff mode.
COBOL_EXTENSIONS = {".cbl", ".cpy", ".jcl", ".pli"}


def load_map() -> dict:
    if not IMPACT_MAP.exists():
        sys.exit(f"Error: {IMPACT_MAP} not found. Run build_impact_map.py first.")
    with IMPACT_MAP.open() as fh:
        return json.load(fh)


def load_dependencies() -> dict:
    if not DEPENDENCIES.exists():
        return {}
    with DEPENDENCIES.open() as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# source-layer search
# --------------------------------------------------------------------------- #

def search(impact_map: dict, name: str) -> list[tuple[str, str, list[dict]]]:
    """
    Return a list of (category_key, matched_key, dependents) tuples.
    One name can appear in multiple categories (e.g. a copybook and a program
    could share a name), so we check all three source categories.
    """
    name_upper = name.upper()
    hits = []
    for category in ("programs", "files", "copybooks"):
        index = impact_map.get(category, {})
        for key, value in index.items():
            if key.upper() == name_upper:
                hits.append((category, key, value["depended_on_by"]))
    return hits


def is_root_program(
    hits: list[tuple[str, str, list[dict]]],
    name: str,
    deps_data: dict,
) -> bool:
    """
    Return True when the queried name is a known program_id but nothing in
    the impact map calls it.

    Two cases:
      1. It appears in impact_map.programs with an empty depended_on_by AND
         has no other category matches that have callers.
      2. It does not appear in impact_map at all but IS a program_id in
         dependencies.json — it is a true entry-point that was never indexed
         as a call target.
    """
    name_upper = name.upper()

    # Case 2: completely absent from the impact map but known as a program
    if not hits:
        return any(
            p.get("program_id", "").upper() == name_upper
            for p in deps_data.get("programs", [])
        )

    # Case 1: present but with no callers
    program_hits     = [h for h in hits if h[0] == "programs"]
    non_program_hits = [h for h in hits if h[0] != "programs"]
    if not program_hits:
        return False
    all_empty = all(len(deps) == 0 for _, _, deps in program_hits)
    any_non_prog_with_callers = any(len(deps) > 0 for _, _, deps in non_program_hits)
    return all_empty and not any_non_prog_with_callers


def downstream_deps_for_program(name: str, deps_data: dict) -> dict:
    """
    Given a program_id, collect the forward dependencies (calls, files,
    copybooks) from dependencies.json for every source file that defines
    that program_id.  Returns a dict ready for JSON output / text display.
    """
    name_upper = name.upper()
    results: dict = {"calls": [], "files": [], "copybooks": []}

    seen_calls:     set[str] = set()
    seen_files:     set[str] = set()
    seen_copybooks: set[str] = set()

    for prog in deps_data.get("programs", []):
        if prog.get("program_id", "").upper() != name_upper:
            continue
        src = prog["file"]

        for call in prog.get("calls", []):
            key = (src, call["program"])
            if key not in seen_calls:
                seen_calls.add(key)
                results["calls"].append({
                    "program":    call["program"],
                    "source_file": src,
                    "confidence": call.get("confidence", "CONFIRMED"),
                    **( {"note": call["note"]} if "note" in call else {} ),
                })

        for f in prog.get("files", []):
            key = (src, f["dd_name"])
            if key not in seen_files:
                seen_files.add(key)
                results["files"].append({
                    "dd_name":    f["dd_name"],
                    "source_file": src,
                    "access":     f.get("access", ""),
                    "confidence": f.get("confidence", "CONFIRMED"),
                })

        for cb in prog.get("copybooks", []):
            key = (src, cb["name"])
            if key not in seen_copybooks:
                seen_copybooks.add(key)
                results["copybooks"].append({
                    "name":       cb["name"],
                    "source_file": src,
                    "confidence": cb.get("confidence", "CONFIRMED"),
                })

    return results


# --------------------------------------------------------------------------- #
# JCL layer helpers
# --------------------------------------------------------------------------- #

def _split_by_confidence(items: list[dict]) -> dict[str, list[dict]]:
    """Group a list of dicts that each carry a 'confidence' key."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        buckets[item.get("confidence", "UNKNOWN")].append(item)
    return dict(buckets)


def jcl_for_programs(jcl: dict, program_ids: set[str]) -> dict[str, list[dict]]:
    """
    Return {program_id: [step_entries]} for every program_id in the set
    that has JCL execution entries.
    """
    executed_by = jcl.get("programs_executed_by", {})
    result = {}
    for pid in program_ids:
        steps = executed_by.get(pid, [])
        if steps:
            result[pid] = steps
    return result


def datasets_for_steps(jcl: dict, step_entries: list[dict]) -> dict[str, dict]:
    """
    Given a list of step entries (from programs_executed_by), collect the
    full dataset metadata (produced_by / consumed_by) for every dataset those
    steps touch.  Returns {dataset_name: {produced_by:[...], consumed_by:[...]}}.
    """
    ds_index = jcl.get("datasets", {})
    touched: set[str] = set()
    for entry in step_entries:
        for ds in entry.get("datasets_input", []):
            touched.add(ds)
        for ds in entry.get("datasets_output", []):
            touched.add(ds)

    result = {}
    for ds in sorted(touched):
        if ds in ds_index:
            result[ds] = ds_index[ds]
        else:
            # dataset referenced in steps but not separately indexed — still show it
            result[ds] = {"produced_by": [], "consumed_by": []}
    return result


def downstream_jobs_for_datasets(jcl: dict, dataset_names: set[str]) -> dict[str, list[dict]]:
    """
    For each dataset that is produced by the affected steps, find which
    downstream jobs/steps consume it.
    Returns {dataset_name: [consumer_step_refs]}.
    """
    downstream_index = jcl.get("dataset_downstream_jobs", {})
    result = {}
    for ds in sorted(dataset_names):
        consumers = downstream_index.get(ds, [])
        if consumers:
            result[ds] = consumers
    return result


def _collect_jcl_data(
    name: str,
    hits: list[tuple[str, str, list[dict]]],
    impact_map: dict,
) -> tuple[dict[str, list[dict]], dict[str, dict], dict[str, list[dict]]]:
    """
    Shared JCL aggregation used by both text and JSON output paths.
    Returns (jcl_hits, ds_details, downstream).
    """
    jcl = impact_map.get("jcl", {})
    if not jcl:
        return {}, {}, {}

    affected_programs: set[str] = set()
    name_upper = name.upper()

    if name_upper in {k.upper() for k in jcl.get("programs_executed_by", {})}:
        affected_programs.add(name_upper)

    for _, _, dependents in hits:
        for dep in dependents:
            affected_programs.add(dep["program_id"].upper())

    exec_index = jcl.get("programs_executed_by", {})
    pid_map = {k.upper(): k for k in exec_index}
    affected_programs_real = {pid_map[p] for p in affected_programs if p in pid_map}

    jcl_hits = jcl_for_programs(jcl, affected_programs_real)

    all_step_entries: list[dict] = []
    all_output_datasets: set[str] = set()
    for steps in jcl_hits.values():
        for s in steps:
            all_step_entries.append(s)
            for ds in s.get("datasets_output", []):
                all_output_datasets.add(ds)

    ds_details  = datasets_for_steps(jcl, all_step_entries)
    downstream  = downstream_jobs_for_datasets(jcl, all_output_datasets)

    return jcl_hits, ds_details, downstream


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #

def build_json_result(
    name: str,
    hits: list[tuple[str, str, list[dict]]],
    impact_map: dict,
    deps_data: dict,
) -> dict:
    """
    Build and return the full blast-radius result as a plain Python dict
    suitable for json.dumps().
    """
    root = is_root_program(hits, name, deps_data)

    result: dict = {
        "query": name.upper(),
        "found": bool(hits) or root,
        "is_root_program": root,
        "source_layer": [],
        "jcl_layer": {
            "jobs_and_steps": [],
            "datasets": [],
            "downstream_jobs": [],
        },
        "total_affected_source_files": 0,
    }

    # ------------------------------------------------------------------ #
    # Root-program case: nothing calls this program — show what it uses
    # ------------------------------------------------------------------ #
    if root:
        result["depends_on"] = downstream_deps_for_program(name, deps_data)

    if not hits:
        return result

    # ------------------------------------------------------------------ #
    # Source layer
    # ------------------------------------------------------------------ #
    total = 0
    for category, matched_key, dependents in hits:
        entry = {
            "category":   category,
            "matched_key": matched_key,
            "label":      CATEGORY_LABELS.get(category, category),
            "dependents": dependents,   # already list[dict] with confidence keys
        }
        result["source_layer"].append(entry)
        total += len(dependents)

    result["total_affected_source_files"] = total

    # ------------------------------------------------------------------ #
    # JCL layer
    # ------------------------------------------------------------------ #
    jcl_hits, ds_details, downstream = _collect_jcl_data(name, hits, impact_map)

    for pid, steps in sorted(jcl_hits.items()):
        result["jcl_layer"]["jobs_and_steps"].append({
            "program_id": pid,
            "steps":      steps,
        })

    for ds_name, detail in ds_details.items():
        result["jcl_layer"]["datasets"].append({
            "dataset":     ds_name,
            "produced_by": detail.get("produced_by", []),
            "consumed_by": detail.get("consumed_by", []),
        })

    for ds_name, consumers in downstream.items():
        result["jcl_layer"]["downstream_jobs"].append({
            "dataset":   ds_name,
            "consumers": consumers,
        })

    return result


# --------------------------------------------------------------------------- #
# text (human-readable) printing
# --------------------------------------------------------------------------- #

def _print_confidence_groups(items: list[dict], fmt_fn) -> None:
    """Print items grouped by confidence level."""
    buckets = _split_by_confidence(items)
    for level in CONFIDENCE_ORDER:
        group = buckets.get(level, [])
        if not group:
            continue
        print(f"    [{level}]")
        for item in group:
            print(f"      {fmt_fn(item)}")
    # catch any unexpected confidence values
    for level, group in buckets.items():
        if level not in CONFIDENCE_ORDER:
            print(f"    [{level}]")
            for item in group:
                print(f"      {fmt_fn(item)}")


def _group_by_name(items: list[dict], name_key: str) -> dict[str, list[dict]]:
    """Group dependency entries by their entity name, preserving first-seen order."""
    grouped: dict[str, list[dict]] = {}
    for item in items:
        key = item[name_key]
        grouped.setdefault(key, []).append(item)
    return grouped


def _print_root_program_deps(name: str, deps: dict) -> None:
    """Print the forward dependencies of a root program, deduplicated by entity."""
    print(f"\n[Root program — nothing calls {name.upper()}]")
    print("  Showing what this program depends on:")

    calls = deps.get("calls", [])
    if calls:
        print("\n  CALLs:")
        for prog_name, entries in _group_by_name(calls, "program").items():
            conf = entries[0].get("confidence", "")
            note = f"  # {entries[0]['note']}" if entries[0].get("note") else ""
            print(f"    [{conf}]  {prog_name}{note}")
            for e in entries:
                print(f"           from {e['source_file']}")

    files = deps.get("files", [])
    if files:
        print("\n  Files (DD names):")
        for dd_name, entries in _group_by_name(files, "dd_name").items():
            conf = entries[0].get("confidence", "")
            access = entries[0].get("access", "")
            print(f"    [{conf}]  {dd_name}  access={access}")
            for e in entries:
                print(f"           from {e['source_file']}")

    copybooks = deps.get("copybooks", [])
    if copybooks:
        print("\n  Copybooks:")
        for cb_name, entries in _group_by_name(copybooks, "name").items():
            conf = entries[0].get("confidence", "")
            print(f"    [{conf}]  {cb_name}")
            for e in entries:
                print(f"           from {e['source_file']}")

    if not calls and not files and not copybooks:
        print("  (no recorded dependencies)")


def print_results(
    name: str,
    hits: list[tuple[str, str, list[dict]]],
    impact_map: dict,
    deps_data: dict,
) -> None:
    root = is_root_program(hits, name, deps_data)

    if not hits and not root:
        print(f"'{name}' was not found in impact_map.json.")
        print("No downstream impact detected (or the name is not tracked).")
        return

    total = sum(len(deps) for _, _, deps in hits)
    print(f"\nBlast radius for: {name.upper()}")
    print("=" * 60)

    # ------------------------------------------------------------------ #
    # Root-program: show what it depends on, then continue with JCL layer
    # ------------------------------------------------------------------ #
    root_deps: dict | None = None
    if root:
        root_deps = downstream_deps_for_program(name, deps_data)
        _print_root_program_deps(name, root_deps)

    if not hits:
        # Pure root program with no reverse-dep hits — show dependency summary
        d = root_deps or {}
        n_calls     = len({c["program"]   for c in d.get("calls",     [])})
        n_files     = len({f["dd_name"]   for f in d.get("files",     [])})
        n_copybooks = len({cb["name"]     for cb in d.get("copybooks", [])})
        print("\n" + "=" * 60)
        print(
            f"Dependencies: {n_calls} program(s) called, "
            f"{n_files} file(s) used, "
            f"{n_copybooks} copybook(s) copied"
        )
        return

    # ------------------------------------------------------------------ #
    # 1. Source-layer: programs / files / copybooks
    # ------------------------------------------------------------------ #
    for category, matched_key, dependents in hits:
        label = CATEGORY_LABELS.get(category, category)
        print(f"\n[{label}: {matched_key}]")
        if not dependents:
            print("  (nothing depends on this)")
        else:
            # Group by confidence; entries without a confidence key go under CONFIRMED
            buckets: dict[str, list[dict]] = {}
            for dep in dependents:
                level = dep.get("confidence", "CONFIRMED")
                buckets.setdefault(level, []).append(dep)
            for level in CONFIDENCE_ORDER:
                group = buckets.get(level, [])
                if not group:
                    continue
                print(f"  [{level}]")
                for dep in group:
                    print(f"    - {dep['file']}  (program_id: {dep['program_id']})")
            for level, group in buckets.items():
                if level not in CONFIDENCE_ORDER:
                    print(f"  [{level}]")
                    for dep in group:
                        print(f"    - {dep['file']}  (program_id: {dep['program_id']})")

    # ------------------------------------------------------------------ #
    # 2. JCL layer
    # ------------------------------------------------------------------ #
    jcl = impact_map.get("jcl", {})
    if not jcl:
        print("\n" + "-" * 60)
        print(f"Total affected source files: {total}")
        return

    jcl_hits, ds_details, downstream = _collect_jcl_data(name, hits, impact_map)

    if jcl_hits:
        print(f"\n{'-' * 60}")
        print("JCL -- Jobs / Steps that execute affected programs")
        print(f"{'-' * 60}")

        for pid, steps in sorted(jcl_hits.items()):
            print(f"\n  Program: {pid}")
            buckets = _split_by_confidence(steps)
            for level in CONFIDENCE_ORDER:
                group = buckets.get(level, [])
                if not group:
                    continue
                print(f"    [{level}]")
                for s in group:
                    inputs  = ", ".join(s.get("datasets_input", []))  or "(none)"
                    outputs = ", ".join(s.get("datasets_output", [])) or "(none)"
                    print(f"      Job {s['job']}  Step {s['step']}  ({s['job_file']})")
                    print(f"        Inputs : {inputs}")
                    print(f"        Outputs: {outputs}")
                    if "note" in s:
                        print(f"        Note   : {s['note']}")

        # ---------------------------------------------------------------- #
        # 3. Dataset layer — produced / consumed by the affected steps
        # ---------------------------------------------------------------- #
        if ds_details:
            print(f"\n{'-' * 60}")
            print("JCL -- Datasets produced / consumed by affected steps")
            print(f"{'-' * 60}")
            for ds_name, detail in ds_details.items():
                print(f"\n  Dataset: {ds_name}")
                produced = detail.get("produced_by", [])
                consumed = detail.get("consumed_by", [])
                if produced:
                    print("    Produced by:")
                    _print_confidence_groups(
                        produced,
                        lambda x: f"Job {x['job']}  Step {x['step']}"
                    )
                if consumed:
                    print("    Consumed by:")
                    _print_confidence_groups(
                        consumed,
                        lambda x: f"Job {x['job']}  Step {x['step']}"
                    )

        # ---------------------------------------------------------------- #
        # 4. Downstream jobs — jobs that consume the output datasets
        # ---------------------------------------------------------------- #
        if downstream:
            print(f"\n{'-' * 60}")
            print("JCL -- Downstream jobs that depend on produced datasets")
            print(f"{'-' * 60}")
            for ds_name, consumers in downstream.items():
                print(f"\n  Dataset: {ds_name}")
                _print_confidence_groups(
                    consumers,
                    lambda x: f"Job {x['job']}  Step {x['step']}"
                )

    print("\n" + "=" * 60)
    print(f"Total affected source files: {total}")


# --------------------------------------------------------------------------- #
# diff / multi-file helpers
# --------------------------------------------------------------------------- #

def _names_from_git_diff() -> list[str]:
    """Return the query name for every COBOL-relevant file reported by
    ``git diff --name-only HEAD~1``.  Files whose extension is not in
    COBOL_EXTENSIONS are silently skipped."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD~1"],
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Error running git diff: {exc.stderr.strip()}")
    except FileNotFoundError:
        sys.exit("Error: 'git' executable not found.")

    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if p.suffix.lower() not in COBOL_EXTENSIONS:
            continue
        names.append(p.stem)
    return names


def _merge_hits(
    all_hits: list[tuple[str, list[tuple[str, str, list[dict]]]]]
) -> list[tuple[str, str, list[dict]]]:
    """Merge hits from multiple queries, deduplicating dependents by file path."""
    # key: (category, matched_key) -> list[dict] (dependents, deduped by 'file')
    merged: dict[tuple[str, str], dict[str, dict]] = {}
    for _name, hits in all_hits:
        for category, matched_key, dependents in hits:
            slot = merged.setdefault((category, matched_key), {})
            for dep in dependents:
                slot[dep["file"]] = dep          # last write wins; values are identical
    return [
        (cat, key, list(deps.values()))
        for (cat, key), deps in merged.items()
    ]


def print_combined_results(
    names: list[str],
    all_hits: list[tuple[str, list[tuple[str, str, list[dict]]]]],
    impact_map: dict,
    deps_data: dict,
) -> None:
    """Print the union blast radius for a set of changed files."""
    merged = _merge_hits(all_hits)
    found_names  = [n for n, hits in all_hits if hits or is_root_program(hits, n, deps_data)]
    missed_names = [n for n, hits in all_hits if not hits and not is_root_program(hits, n, deps_data)]

    header = "Combined blast radius for: " + ", ".join(n.upper() for n in names)
    print(f"\n{header}")
    print("=" * max(60, len(header)))

    if missed_names:
        print("\n  [NOT FOUND] " + ", ".join(n.upper() for n in missed_names))

    if not merged and not found_names:
        print("No downstream impact detected for any of the supplied names.")
        return

    # Reuse single-query printing per entry so formatting is identical
    for name, hits in all_hits:
        if hits or is_root_program(hits, name, deps_data):
            print(f"\n{'-' * 60}")
            print(f"  -> {name.upper()}")
            print_results(name, hits, impact_map, deps_data)

    # Deduplicated union summary
    total_unique = len({dep["file"] for _, hits in all_hits for _, _, deps in hits for dep in deps})
    print("\n" + "=" * 60)
    print(f"Total unique affected source files (union): {total_unique}")


def build_combined_json_result(
    names: list[str],
    all_hits: list[tuple[str, list[tuple[str, str, list[dict]]]]],
    impact_map: dict,
    deps_data: dict,
) -> dict:
    """Build a JSON result for the combined blast radius of multiple changed files."""
    merged = _merge_hits(all_hits)
    total_unique = len({dep["file"] for _, hits in all_hits for _, _, deps in hits for dep in deps})

    per_name = []
    for name, hits in all_hits:
        entry = build_json_result(name, hits, impact_map, deps_data)
        per_name.append(entry)

    return {
        "mode":    "diff",
        "queries": [n.upper() for n in names],
        "per_name_results": per_name,
        "union": {
            "source_layer": [
                {
                    "category":    cat,
                    "matched_key": key,
                    "label":       CATEGORY_LABELS.get(cat, cat),
                    "dependents":  deps,
                }
                for cat, key, deps in merged
            ],
            "total_affected_source_files": total_unique,
        },
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Show the blast radius of a COBOL program, DD file, or copybook. "
            "Use --changed or --git-diff for multi-file diff mode."
        ),
        add_help=True,
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Program ID, DD file name, or copybook name to query.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output the result as structured JSON instead of formatted text.",
    )
    parser.add_argument(
        "--changed",
        nargs="+",
        metavar="FILE",
        help="Compute the combined blast radius across these changed files/names.",
    )
    parser.add_argument(
        "--git-diff",
        action="store_true",
        dest="git_diff",
        help="Read changed files from 'git diff --name-only HEAD~1' and compute their combined blast radius.",
    )
    args = parser.parse_args()

    # Validate mutually exclusive usage
    diff_flags = sum([bool(args.changed), args.git_diff])
    if diff_flags > 1:
        parser.error("--changed and --git-diff are mutually exclusive.")
    if diff_flags > 0 and args.name:
        parser.error("'name' positional argument cannot be combined with --changed or --git-diff.")
    if diff_flags == 0 and not args.name:
        parser.error("A name argument is required (or use --changed / --git-diff).")

    impact_map = load_map()
    deps_data  = load_dependencies()

    # ------------------------------------------------------------------ #
    # Diff / multi-file mode
    # ------------------------------------------------------------------ #
    if args.changed or args.git_diff:
        if args.git_diff:
            names = _names_from_git_diff()
            if not names:
                print("No changed files reported by git diff HEAD~1.")
                sys.exit(0)
        else:
            # Keep only COBOL-relevant files; use the stem as the query name.
            # (.cbl → program name, .cpy → copybook name, .jcl/.pli → stem)
            names = [
                Path(f).stem
                for f in args.changed
                if Path(f).suffix.lower() in COBOL_EXTENSIONS
            ]
            if not names:
                print("No COBOL-relevant files supplied to --changed.")
                sys.exit(0)

        all_hits = [(name, search(impact_map, name)) for name in names]

        if len(names) == 1:
            name, hits = all_hits[0]
            if args.output_json:
                result = build_json_result(name, hits, impact_map, deps_data)
                print(json.dumps(result, indent=2))
            else:
                print_results(name, hits, impact_map, deps_data)
            return

        if args.output_json:
            result = build_combined_json_result(names, all_hits, impact_map, deps_data)
            print(json.dumps(result, indent=2))
        else:
            print_combined_results(names, all_hits, impact_map, deps_data)
        return

    # ------------------------------------------------------------------ #
    # Single-name mode (original behaviour)
    # ------------------------------------------------------------------ #
    hits = search(impact_map, args.name)

    if args.output_json:
        result = build_json_result(args.name, hits, impact_map, deps_data)
        print(json.dumps(result, indent=2))
    else:
        print_results(args.name, hits, impact_map, deps_data)


if __name__ == "__main__":
    main()
