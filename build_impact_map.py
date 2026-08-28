"""
build_impact_map.py
Reads dependencies.json and produces impact_map.json — a reverse-dependency
index that, for every program_id, dd_name (file), and copybook name, lists
every source file (and program_id) that depends on it.
"""

import json
from collections import defaultdict

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _entry():
    return {"depended_on_by": []}

def _ref(file_path, program_id):
    return {"file": file_path, "program_id": program_id}

# --------------------------------------------------------------------------- #
# main build
# --------------------------------------------------------------------------- #

with open("dependencies.json") as fh:
    data = json.load(fh)

programs_index   = defaultdict(_entry)   # program_id  -> {depended_on_by:[...]}
files_index      = defaultdict(_entry)   # dd_name     -> {depended_on_by:[...]}
copybooks_index  = defaultdict(_entry)   # copybook    -> {depended_on_by:[...]}

for prog in data["programs"]:
    src   = prog["file"]
    pid   = prog["program_id"]
    ref   = _ref(src, pid)

    # programs this source file CALLs
    for call in prog.get("calls", []):
        programs_index[call["program"]]["depended_on_by"].append(ref)

    # files (DD names) this source file opens
    for f in prog.get("files", []):
        files_index[f["dd_name"]]["depended_on_by"].append(ref)

    # copybooks this source file COPYs
    for cb in prog.get("copybooks", []):
        copybooks_index[cb["name"]]["depended_on_by"].append(ref)

impact_map = {
    "programs":  dict(programs_index),
    "files":     dict(files_index),
    "copybooks": dict(copybooks_index),
}

with open("impact_map.json", "w") as fh:
    json.dump(impact_map, fh, indent=2)

print("impact_map.json written.")
