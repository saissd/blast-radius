# IBM Bob — Usage Statement

## Overview

IBM Bob in Agent mode was used throughout the construction of Blast Radius. Every source file in the project — `dependencies.json`, `build_impact_map.py`, `blast_radius.py`, `risk_score.py`, and `.github/workflows/blast-radius.yml` — was written or significantly shaped in Bob Agent mode sessions.

The dependency manifest was expanded in a subsequent session to incorporate the openmainframeproject/cobol-programming-course source tree, growing the corpus from 2 COBOL programs and 5 JCL jobs to 12 source files, 42 JCL jobs, and 87 JCL dataset edges. Bob read the course's JCL and COBOL files, extended `dependencies.json`, and regenerated `impact_map.json` without modifying any existing entry.

## The Four Named Subagents

The architecture of `dependencies.json` is built around four named subagents declared in the file's `_agents` metadata field:

```json
"_agents": ["CallGraphAgent", "CopybookAgent", "DataFlowAgent", "JCLAgent"]
```

Each subagent had a focused, well-scoped responsibility:

| Subagent | Responsibility |
|---|---|
| **CallGraphAgent** | Resolved `CALL` statements — both static literal calls and identifier-based dynamic calls — to concrete program IDs |
| **CopybookAgent** | Resolved every `COPY` statement to a physical file path, including library-qualified `IN lib` forms |
| **DataFlowAgent** | Extracted every `SELECT…ASSIGN` / `OPEN` pair and recorded the DD name, logical name, and access mode |
| **JCLAgent** | Parsed JCL jobs, steps, `EXEC PGM=`, dataset DDs, `INCLUDE` members, and `EXEC PROC=` calls; emitted step-level dataset edges |

Running these as parallel, independently-scoped subagents in Bob prevented context contamination between layers (call graph reasoning is unrelated to JCL parsing) and allowed each agent to develop specialised reasoning about its own source artifacts.

## Reasoning About Dynamic CALL Statements

The most non-trivial case Bob handled was dynamic `CALL` statements resolved through working-storage variables. A naive regex over the source line:

```cobol
CALL SAM2
```

would appear to be a static call to a program named `SAM2`. It is not. `SAM2` here is a working-storage identifier — a `PIC X(8)` variable initialised with `VALUE 'SAM2'` at line 122 of `SAM1.cbl`. The actual call target is only known at runtime.

Bob's CallGraphAgent traced this correctly: it identified the identifier reference, located the working-storage declaration, extracted the literal value, and produced a `NEEDS_REVIEW` confidence record rather than a `CONFIRMED` one. The `dependencies.json` note field for this edge reads:

> "CALL uses identifier SAM2 defined as working-storage variable (PIC X(8) VALUE 'SAM2') at line 122; target resolves at runtime"

A regex-based scanner matching on `CALL\s+"([^"]+)"` would have missed this edge entirely. The same pattern recurs in `SAM1LIB.cbl` at line 287 / working-storage line 105, and Bob identified both independently.

This reasoning — recognise an identifier reference, trace it back to its declaration, inspect the `VALUE` clause, classify the confidence — is exactly the kind of multi-step semantic inference that flat pattern matching cannot perform.

## Agent Mode

The entire build used Bob's Agent mode. Agent mode was appropriate because:

- Writing `blast_radius.py` and `risk_score.py` required iterative code generation, reading the evolving `dependencies.json` schema, and adjusting logic as the schema was refined.
- The CI workflow required cross-file reasoning (the workflow script must match the exit codes and output format of `risk_score.py`).
- Debugging the root-program scoring path (where an entity with no callers must pivot to scoring its own downstream dependencies) required reading the impact map structure and the scorer logic in the same context.

## Session Screenshots (`bob_sessions/`)

The `bob_sessions/` directory contains eight PNG screenshots, each capturing a Bob task panel showing the Task Id, Bobcoins consumed, context length, and workspace (`blast-radius`). They document the sequence of Agent mode tasks used to build the project.

| File | Task Id (prefix) | Bobcoins | What the session shows |
|---|---|---|---|
| `bedrock_task01_risk_scorer.png` | `b513e3c9` | 0.790 | Writing `risk_score.py` — notable design choices panel: OUTPUT-only file risk, shared copybook definition (two or more distinct `program_id`s), clean `--json` shape, and score-0 / LOW for unknown entities |
| `bedrock_task02_ci_gate.png` | `b513e3c9` | 2.55 | Writing `.github/workflows/blast-radius.yml` — "All tasks completed 2/2"; step-flow table documents the `changed`, `gate`, and `Post risk summary comment` steps with their logic |
| `bedrock_task03_jcl_investigation.png` | `b513e3c9` | 7.17 | Debugging a missing JCL weight — "All tasks completed 2/2"; scoring table shows JCL jobs (ZDERUN) ×6 contribution was absent from the score (total 34 → MEDIUM instead of the expected higher value); Bob diagnosed the root cause as a stale local `main` branch whose `impact_map.json` was missing the `jcl` block (`programs_executed_by`, `datasets`, `dataset_downstream_jobs`); resolved by pulling updated `main` |
| `bedrock_task04_recalibrate.png` | `b513e3c9` | 7.17 | Adding the root-program scoring path — "All tasks completed 2/2"; explains `is_root_program` + `_root_program_facts` logic: SAM1 has no entry in `impact_map.programs` so scoring inverts direction, counting what SAM1 *depends on*; SAM1 calls SAM2, writes 2 files, uses copybooks, runs in ZDERUN → score 71, HIGH |
| `bedrock_task05_report_flag.png` | `d17f56ef` | 0.694 | Adding `--report` flag — task prompt visible: add `--report` to `risk_score.py` for GitHub PR comment Markdown, then update the workflow to use `--report` output instead of building the comment inline; "All tasks completed 2/2"; 2 files changed |
| `bedrock_task06_band_distribution.png` | `f3b113d6` | 0.861 | Recalibrating scoring weights — task prompt visible: scores were clustering at 74/74/71 then dropping to 21 with nothing in MEDIUM; Bob tuned weights so SAM2 lands at 40 (MEDIUM, 40–50 target), CUSTCOPY/TRANREC/SAM1 remain HIGH, SAM2PARM promotes to HIGH as a shared copybook used across SAM1 and SAM2 in the multiroot variant; 1 file changed |
| `bedrock_task07_readme.png` | `71e3515a` | 0.646 | Writing `README.md` — task panel shows the subagent responsibility table (CopybookAgent, DataFlowAgent, JCLAgent visible), usage examples for every tool and flag, and DATA_SOURCES credits to `github.com/IBM/zopeneditor-sample` |
| `bedrock_task08_demo_scripts.png` | `feaa97a6` | 1.39 | Fixing demo scripts — Bob reads `sample-cobol/COBOL/SAM1.cbl` to resolve a `COPY CUSTCOPY REPLACING ==:TAG:== BY ==CUST==` token, determines the expanded field name is `CUST-ACCT-BALANCE` (working storage `WS-CUST-ACCT-BALANCE`, file record `CUST-ACCT-BALANCE`), then applies targeted fixes to both scripts; 2 files changed in 2 ms |

All eight screenshots share workspace `blast-radius`. Tasks 1–4 share Task Id prefix `b513e3c9`, indicating they were continuations within the same Bob session. Tasks 5, 6, 7, and 8 each carry a distinct Task Id, indicating they were opened as separate sessions.
