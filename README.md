# Blast Radius

> *COBOL modernization projects run, on average, **2.5× over budget**. The leading cause is undocumented dependencies — a copybook shared by a dozen programs, a DD file that feeds three downstream jobs, a called subprogram that nobody knew existed until production broke.*

Blast Radius is a dependency-mapping and change-risk toolkit for mainframe COBOL codebases. It statically parses COBOL source, copybooks, and JCL, builds a reverse-dependency index, and answers the question: **if I change this program, file, or copybook, what else breaks?**

---

## The Problem

Enterprise COBOL systems are vast, intertwined, and largely undocumented at the dependency level. The same copybook may be `COPY`'d by twenty programs. A single `CALL` statement may resolve at runtime from a working-storage variable. A JCL job may quietly consume a dataset produced by a completely separate job in a different team's namespace.

When a modernization team touches `CUSTCOPY.cpy`, they don't know:
- Which programs recompile.
- Which output files are written by those programs.
- Which JCL jobs run those programs.
- Which downstream jobs depend on the datasets those jobs produce.

Blast Radius traces all four of those layers, flags confidence (CONFIRMED vs. NEEDS_REVIEW), and emits a weighted risk score so CI can block a pull request before the damage reaches production.

---

## Architecture

The system is built in three layers:

```
dependencies.json          ← hand-curated / agent-generated source of truth
       │
       ▼
build_impact_map.py        ← inverts the graph into a reverse-dependency index
       │
       ▼
impact_map.json            ← four indexes: programs · files · copybooks · jcl
       │
       ├──▶ blast_radius.py   ← interactive blast-radius explorer
       └──▶ risk_score.py     ← weighted 0-100 risk scorer + CI gate
```

### `dependencies.json` — the dependency manifest

Produced by four named subagents that parse the source tree:

| Subagent | Responsibility |
|---|---|
| **CallGraphAgent** | Resolves `CALL` statements (static and identifier-based dynamic calls) to concrete program IDs |
| **CopybookAgent** | Resolves every `COPY` statement to a physical file path, including library-qualified `IN lib` forms |
| **DataFlowAgent** | Extracts every `SELECT…ASSIGN` / `OPEN` pair and records the DD name, logical name, and access mode (INPUT / OUTPUT) |
| **JCLAgent** | Parses JCL jobs, steps, `EXEC PGM=`, dataset DDs, `INCLUDE` members, and `EXEC PROC=` calls; emits step-level dataset edges |

Each dependency carries a `confidence` field — `CONFIRMED` (statically certain) or `NEEDS_REVIEW` (runtime-resolved or library-symbolic).

### `build_impact_map.py` — graph inversion

Reads `dependencies.json` and produces `impact_map.json` with four sections:

- **`programs`** — for every called program ID, lists every source file that calls it
- **`files`** — for every DD name, lists every source file that opens it
- **`copybooks`** — for every copybook name, lists every source file that copies it
- **`jcl`** — three sub-indexes: `programs_executed_by`, `datasets`, `dataset_downstream_jobs`

### `blast_radius.py` — dependency explorer

Queries the impact map for a given program, DD file, or copybook name and traces the full four-layer blast radius: source files → programs → JCL jobs → datasets. Root programs (entry-points that nothing calls) are handled specially: the tool shows what *they depend on* rather than returning an empty result.

### `risk_score.py` — change-risk scorer

Computes a weighted 0–100 risk score for any entity using:

| Signal | Weight |
|---|---|
| Each affected program | ×5 |
| Each OUTPUT file written | ×3 |
| Each copybook use (source files that COPY it) | ×8 |
| Each JCL job affected | ×5 |
| Program category base (call-chain risk premium) | +25 |

**Bands:** HIGH ≥ 60 · MEDIUM 30–59 · LOW < 30

Supports `--json` (structured output), `--report` (Markdown for PR comments), and `--gate` (exit code 1 on HIGH, for CI).

#### `risk_scores.json` — pre-generated snapshot

`risk_scores.json` is a **generated snapshot** produced by running `risk_score.py --json` across every entity and collecting the results. It is consumed by the dependency graph UI (`index.html`) so the browser does not need to run Python at visualisation time.

> **Important:** `risk_scores.json` must be regenerated whenever the scoring weights in `risk_score.py` change, or whenever `dependencies.json` / `impact_map.json` are updated. If it is out of date, the UI will display stale risk scores and regression-test lists.

To regenerate it:

```bash
python build_risk_scores.py        # if a bulk-export script exists, or:
python risk_score.py --json SAM1   # per-entity, pipe/collect into risk_scores.json
```

### `.github/workflows/blast-radius.yml` — CI gate

Triggers on any pull request that touches `*.cbl`, `*.cpy`, or `*.jcl`. For each changed file it derives the entity name, runs `risk_score.py --report` to generate a Markdown block, and runs `risk_score.py --gate` to determine pass/fail. A summary comment is posted to the PR; the job fails if any entity is HIGH.

---

## Setup

No third-party dependencies. Python 3.11+ and the standard library are sufficient.

```bash
# (Re)generate the impact map after editing dependencies.json
python build_impact_map.py
# impact_map.json written.
```

---

## Usage

### `blast_radius.py` — explore the blast radius of any entity

```
python blast_radius.py <name>
python blast_radius.py --changed <file1> [<file2> ...]
python blast_radius.py --json <name>
python blast_radius.py --git-diff
```

**Query a called subprogram — `SAM2`**

```
$ python blast_radius.py SAM2

Blast radius for: SAM2
============================================================

[Programs that CALL: SAM2]
  [NEEDS_REVIEW]
    - sample-cobol/COBOL/SAM1.cbl  (program_id: SAM1)
    - sample-cobol/COBOL/SAM1LIB.cbl  (program_id: SAM1)
    - sample-cobol/multiroot/sam/SAM1.cbl  (program_id: SAM1)

------------------------------------------------------------
JCL -- Jobs / Steps that execute affected programs
------------------------------------------------------------

  Program: SAM1
    [CONFIRMED]
      Job ZDERUN  Step SAM1  (sample-cobol/JCL/RUN.jcl)
        Inputs : &HLQ..SAMPLE.LOAD(SAM1), &HLQ..SAMPLE.CUSTFILE, &HLQ..SAMPLE.TRANFILE
        Outputs: &HLQ..SAMPLE.CUSTOUT, &HLQ..SAMPLE.CUSTRPT
        Note   : Execute SAM1 application

------------------------------------------------------------
JCL -- Datasets produced / consumed by affected steps
------------------------------------------------------------

  Dataset: &HLQ..SAMPLE.CUSTFILE
    Produced by:
    [CONFIRMED]
      Job ZDEALLC  Step DELETE
    Consumed by:
    [CONFIRMED]
      Job ZDERUN  Step SAM1
  ...

============================================================
Total affected source files: 3
```

**Query a shared copybook — `CUSTCOPY`**

```
$ python blast_radius.py CUSTCOPY

Blast radius for: CUSTCOPY
============================================================

[Programs that COPY: CUSTCOPY]
  [CONFIRMED]
    - sample-cobol/COBOL/SAM1.cbl  (program_id: SAM1)
    - sample-cobol/COBOL/SAM1LIB.cbl  (program_id: SAM1)
    - sample-cobol/COBOL/SAM2.cbl  (program_id: SAM2)
    - sample-cobol/multiroot/sam/SAM1.cbl  (program_id: SAM1)
    - sample-cobol/multiroot/sam/SAM2.cbl  (program_id: SAM2)

------------------------------------------------------------
JCL -- Jobs / Steps that execute affected programs
------------------------------------------------------------

  Program: SAM1
    [CONFIRMED]
      Job ZDERUN  Step SAM1  (sample-cobol/JCL/RUN.jcl)
        ...

============================================================
Total affected source files: 5
```

**Query a root (entry-point) program — `SAM1`**

Root programs have no callers. The tool pivots to show downstream dependencies instead.

```
$ python blast_radius.py SAM1

Blast radius for: SAM1
============================================================

[Root program — nothing calls SAM1]
  Showing what this program depends on:

  CALLs:
    [NEEDS_REVIEW]  SAM2  # CALL uses identifier SAM2 defined as working-storage variable ...

  Files (DD names):
    [CONFIRMED]  CUSTFILE  access=INPUT
    [CONFIRMED]  TRANFILE  access=INPUT
    [CONFIRMED]  CUSTOUT   access=OUTPUT
    [CONFIRMED]  CUSTRPT   access=OUTPUT

  Copybooks:
    [CONFIRMED]      CUSTCOPY
    [CONFIRMED]      TRANREC
    [NEEDS_REVIEW]   DATETIME
    [NEEDS_REVIEW]   REPTTOTL
    [CONFIRMED]      SAM2PARM

============================================================
Dependencies: 1 program(s) called, 4 file(s) used, 5 copybook(s) copied
```

**Multi-file combined blast radius — useful for `--changed` or `--git-diff`**

```
$ python blast_radius.py --changed sample-cobol/COBOL/SAM2.cbl \
                                    sample-cobol/COPYBOOK/CUSTCOPY.cpy

Combined blast radius for: SAM2, CUSTCOPY
============================================================
  -> SAM2  (3 affected source files)
  -> CUSTCOPY  (5 affected source files)

============================================================
Total unique affected source files (union): 5
```

---

### `risk_score.py` — score the change risk of any entity

> **`risk_scores.json` is a generated snapshot.** Run `risk_score.py --json` across all entities and write the results to `risk_scores.json` to keep the UI current. Regenerate it whenever the scoring weights in `risk_score.py` change or the UI will display stale risk numbers.

```
python risk_score.py [--json] [--report] [--gate] <name>
```

**Plain-text output**

```
$ python risk_score.py SAM2

Entity  : SAM2
Score   : 41/100
Band    : MEDIUM
Reason  : Changing the program 'SAM2' impacts: 1 affected program (SAM1);
          2 output files written (CUSTOUT, CUSTRPT); 1 JCL job affected (ZDERUN);
          +25 program category base. Raw weighted score 41.
Regression tests : SAM1
```

```
$ python risk_score.py CUSTCOPY

Entity  : CUSTCOPY
Score   : 61/100
Band    : HIGH
Reason  : Changing the copybook 'CUSTCOPY' impacts: 2 affected programs (SAM1, SAM2);
          2 output files written (CUSTOUT, CUSTRPT); 5 source files COPY it;
          1 JCL job affected (ZDERUN). Raw weighted score 61.
Regression tests : SAM1, SAM2
```

```
$ python risk_score.py SAM1

Entity  : SAM1
Score   : 81/100
Band    : HIGH
Reason  : 'SAM1' is an entry-point program (nothing calls it). It calls 1 program (SAM2);
          writes 2 output files (CUSTOUT, CUSTRPT); uses 5 copybooks;
          executed by 1 JCL job (ZDERUN). Raw weighted score 81.
Regression tests : SAM1, SAM2
```

**JSON output (`--json`)**

```
$ python risk_score.py --json SAM2

{
  "entity": "SAM2",
  "score": 41,
  "band": "MEDIUM",
  "reason": "Changing the program 'SAM2' impacts: 1 affected program (SAM1); ...",
  "regression_tests": [
    "SAM1"
  ]
}
```

**Markdown report for PR comments (`--report`)**

```
$ python risk_score.py --report CUSTCOPY

### `CUSTCOPY` — 🔴 HIGH

**Score:** 61/100

**Reason:** Changing the copybook 'CUSTCOPY' impacts: 2 affected programs (SAM1, SAM2);
2 output files written (CUSTOUT, CUSTRPT); 5 source files COPY it;
1 JCL job affected (ZDERUN). Raw weighted score 61.

**Regression tests:**

- `SAM1`
- `SAM2`
```

**CI gate (`--gate`)**

`--gate` exits with code `1` if the band is HIGH, `0` otherwise. Combine with `--report` to emit the Markdown block AND set the exit code:

```bash
python risk_score.py --gate --report CUSTCOPY
# exits 1 — HIGH risk
```

---

### `build_impact_map.py` — regenerate the impact map

Run this whenever `dependencies.json` is updated:

```
$ python build_impact_map.py
impact_map.json written.
```

---

## CI Integration

The included GitHub Actions workflow (`.github/workflows/blast-radius.yml`) runs automatically on any PR that modifies `*.cbl`, `*.cpy`, or `*.jcl` files.

**What it does:**

1. Detects all changed mainframe source files in the PR diff.
2. Derives the entity name from each file path (strips directory and extension).
3. Runs `risk_score.py --report` and `--gate` for each entity.
4. Posts a consolidated Markdown comment to the PR with every score and regression test list.
5. Fails the workflow check if any entity is HIGH — blocking merge.

**Example PR comment (blocked):**

```
## ⛔ Blast-Radius Risk Gate — BLOCKED

One or more changed entities scored HIGH risk.
The following programs need retesting before this PR can merge:

  CUSTCOPY

---

## Risk breakdown

### `CUSTCOPY` — 🔴 HIGH
Score: 61/100
...
```

**No secrets required.** The workflow uses only `GITHUB_TOKEN` (automatically available) and the standard library.

---

## File Reference

| File | Purpose |
|---|---|
| `dependencies.json` | Source-of-truth dependency manifest (schema v2) |
| `impact_map.json` | Generated reverse-dependency index (do not edit directly) |
| `build_impact_map.py` | Regenerates `impact_map.json` from `dependencies.json` |
| `blast_radius.py` | Interactive dependency explorer |
| `risk_score.py` | Weighted risk scorer with CI gate support |
| `risk_scores.json` | **Generated snapshot** — pre-computed risk scores for all entities, consumed by `index.html`; must be regenerated when scoring weights or source data change |
| `index.html` | Interactive dependency graph UI (loads `dependencies.json`, `impact_map.json`, `risk_scores.json`) |
| `.github/workflows/blast-radius.yml` | GitHub Actions PR gate |
| `sample-cobol/` | IBM z Open Editor sample COBOL programs (see DATA_SOURCES) |

---

## DATA_SOURCES

The COBOL source programs, copybooks, and JCL used as the reference codebase in this repository are sourced from:

**IBM z Open Editor Sample**
Repository: [https://github.com/IBM/zopeneditor-sample](https://github.com/IBM/zopeneditor-sample)
Local path: `sample-cobol/`

This includes:
- `sample-cobol/COBOL/SAM1.cbl` — main entry-point COBOL program
- `sample-cobol/COBOL/SAM1LIB.cbl` — library-copybook variant of SAM1
- `sample-cobol/COBOL/SAM2.cbl` — subprogram called dynamically by SAM1
- `sample-cobol/COPYBOOK/CUSTCOPY.cpy`, `TRANREC.cpy` — shared copybooks
- `sample-cobol/COPYLIB/DATETIME.cpy`, `COPYLIB-MVS/REPTTOTL.cpy` — library-qualified copybooks
- `sample-cobol/multiroot/` — multiroot workspace variant (V02) with extended SAM2PARM linkage
- `sample-cobol/JCL/RUN.jcl`, `RUNASAM1.jcl`, `RUNPSAM1.jcl`, `ALLOCATE.jcl`, `INCLUDE.jcl` — JCL job streams
- `sample-cobol/PLI/` — PL/I companion programs (PSAM1, PSAM2)
- `sample-cobol/ASM/` — Assembler companion program (ASAM1)

The sample programs implement a simple customer-file batch application: `SAM1` reads `CUSTFILE` and `TRANFILE`, calls `SAM2` to process individual transactions, and writes updated records to `CUSTOUT` and a report to `CUSTRPT`.

All source files remain unmodified from the upstream IBM repository and are used here under their original license (see `sample-cobol/LICENSE`).
