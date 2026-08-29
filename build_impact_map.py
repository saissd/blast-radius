"""
build_impact_map.py
Reads dependencies.json and produces impact_map.json — a reverse-dependency
index that, for every program_id, dd_name (file), and copybook name, lists
every source file (and program_id) that depends on it.

Also builds a jcl section with three sub-indexes:
  programs_executed_by  – maps program_id -> steps that execute that program
  datasets              – maps dataset name -> {produced_by, consumed_by} steps
  dataset_downstream_jobs – maps dataset -> jobs that consume it (for chaining)
"""

import json
from collections import defaultdict

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _entry():
    return {"depended_on_by": []}

def _ref(file_path, program_id, confidence=None):
    r = {"file": file_path, "program_id": program_id}
    if confidence:
        r["confidence"] = confidence
    return r

def _step_ref(job_name, step_name, confidence, note=None):
    r = {"job": job_name, "step": step_name, "confidence": confidence}
    if note:
        r["note"] = note
    return r

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

    # programs this source file CALLs
    for call in prog.get("calls", []):
        programs_index[call["program"]]["depended_on_by"].append(
            _ref(src, pid, call.get("confidence"))
        )

    # files (DD names) this source file opens
    for f in prog.get("files", []):
        files_index[f["dd_name"]]["depended_on_by"].append(
            _ref(src, pid, f.get("confidence"))
        )

    # copybooks this source file COPYs
    for cb in prog.get("copybooks", []):
        copybooks_index[cb["name"]]["depended_on_by"].append(
            _ref(src, pid, cb.get("confidence"))
        )

# --------------------------------------------------------------------------- #
# JCL layer
# --------------------------------------------------------------------------- #

# programs_executed_by: program_id -> list of step references
jcl_programs_index = defaultdict(list)

# datasets: dataset_name -> {produced_by: [...], consumed_by: [...]}
jcl_datasets_index = defaultdict(lambda: {"produced_by": [], "consumed_by": []})

for job in data.get("jcl", {}).get("jobs", []):
    job_name = job["job_name"]
    # Confidence is CONFIRMED for all steps (the JCL is static/parsed)
    confidence = "CONFIRMED"

    for step in job.get("steps", []):
        step_name = step["step_name"]
        program   = step["program"]
        note      = step.get("note")

        # index which steps execute this program
        jcl_programs_index[program].append({
            "job":            job_name,
            "job_file":       job["file"],
            "step":           step_name,
            "datasets_input":  step.get("datasets_input", []),
            "datasets_output": step.get("datasets_output", []),
            "confidence":     confidence,
            **({"note": note} if note else {}),
        })

        # index dataset production/consumption per step
        for ds in step.get("datasets_input", []):
            jcl_datasets_index[ds]["consumed_by"].append(
                _step_ref(job_name, step_name, confidence)
            )
        for ds in step.get("datasets_output", []):
            jcl_datasets_index[ds]["produced_by"].append(
                _step_ref(job_name, step_name, confidence)
            )

# dataset_downstream_jobs: dataset -> list of {job, step, confidence} that consume it
# (derived directly from jcl_datasets_index, but surfaced as a flat list for easy lookup)
jcl_dataset_downstream = {}
for ds, val in jcl_datasets_index.items():
    if val["consumed_by"]:
        jcl_dataset_downstream[ds] = val["consumed_by"]

# --------------------------------------------------------------------------- #
# assemble and write
# --------------------------------------------------------------------------- #

impact_map = {
    "programs":  dict(programs_index),
    "files":     dict(files_index),
    "copybooks": dict(copybooks_index),
    "jcl": {
        "programs_executed_by":   dict(jcl_programs_index),
        "datasets":               dict(jcl_datasets_index),
        "dataset_downstream_jobs": jcl_dataset_downstream,
    },
}

with open("impact_map.json", "w") as fh:
    json.dump(impact_map, fh, indent=2)

print("impact_map.json written.")
