#!/usr/bin/env python3
"""
blast_radius.py
Usage:
    python blast_radius.py <name>

<name> is matched case-insensitively against:
  - program IDs   (e.g. SAM2)
  - DD file names (e.g. CUSTFILE)
  - copybook names (e.g. CUSTCOPY)

Prints every source file / program that would be affected if <name> changed,
grouped by category.  Uses impact_map.json (must be in the same directory).
"""

import json
import sys
from pathlib import Path

IMPACT_MAP = Path(__file__).parent / "impact_map.json"

CATEGORY_LABELS = {
    "programs":  "Programs that CALL",
    "files":     "Programs that open file (DD)",
    "copybooks": "Programs that COPY",
}


def load_map() -> dict:
    if not IMPACT_MAP.exists():
        sys.exit(f"Error: {IMPACT_MAP} not found. Run build_impact_map.py first.")
    with IMPACT_MAP.open() as fh:
        return json.load(fh)


def search(impact_map: dict, name: str) -> list[tuple[str, str, list[dict]]]:
    """
    Return a list of (category_key, matched_key, dependents) tuples.
    One name can appear in multiple categories (e.g. a copybook and a program
    could share a name), so we check all three.
    """
    name_upper = name.upper()
    hits = []
    for category, index in impact_map.items():
        for key, value in index.items():
            if key.upper() == name_upper:
                hits.append((category, key, value["depended_on_by"]))
    return hits


def print_results(name: str, hits: list[tuple[str, str, list[dict]]]) -> None:
    if not hits:
        print(f"'{name}' was not found in impact_map.json.")
        print("No downstream impact detected (or the name is not tracked).")
        return

    total = sum(len(deps) for _, _, deps in hits)
    print(f"\nBlast radius for: {name.upper()}")
    print("-" * 50)

    for category, matched_key, dependents in hits:
        label = CATEGORY_LABELS.get(category, category)
        print(f"\n[{label}: {matched_key}]")
        if not dependents:
            print("  (nothing depends on this)")
        else:
            for dep in dependents:
                print(f"  - {dep['file']}  (program_id: {dep['program_id']})")

    print("\n" + "-" * 50)
    print(f"Total affected source files: {total}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    name = sys.argv[1]
    impact_map = load_map()
    hits = search(impact_map, name)
    print_results(name, hits)


if __name__ == "__main__":
    main()
