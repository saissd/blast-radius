# Blast Radius — Submission

## The Problem

Roughly 800 billion lines of COBOL are in active use, running an estimated $3 trillion in daily commerce. The average COBOL programmer is 55 and retiring. Modernization projects run 2.5× over budget — not because of bad code, but because dependencies go unmapped.

The same copybook may be `COPY`'d by twenty programs. A `CALL` statement may resolve at runtime from a working-storage variable, invisible to grep. A JCL job may consume a dataset produced by a job in another team's namespace. When a team touches a shared copybook, they find out which programs break in production.

## What Blast Radius Is

Blast Radius is a dependency-mapping and change-risk toolkit for mainframe COBOL codebases. It statically parses COBOL source, copybooks, and JCL, builds a reverse-dependency index, and answers: *if I change this program, file, or copybook — what else breaks?*

A dependency manifest (`dependencies.json`) records every `CALL`, `COPY`, `SELECT…ASSIGN`, and JCL dataset edge. `build_impact_map.py` inverts that graph into a reverse index across programs, DD files, copybooks, and JCL jobs. `blast_radius.py` traces the four-layer blast radius for any entity; `risk_score.py` produces a 0–100 risk score and a HIGH / MEDIUM / LOW band. On any PR touching `*.cbl`, `*.cpy`, or `*.jcl`, a GitHub Actions workflow scores every changed entity, posts a Markdown breakdown as a PR comment, and blocks merge if any entity scores HIGH.

The corpus covers 12 source files, 42 JCL jobs, and 87 dataset edges across two repositories.

## Target Users

Mainframe modernization engineers, platform architects, and DevOps teams at banks, insurers, and government agencies. Secondary users are change-advisory boards and release managers needing a machine-readable risk signal before approving changes. No mainframe runtime required — Python 3.11 and the standard library only.

## How Users Interact With It

A developer queries the blast radius of anything they are about to change:

```
python blast_radius.py CUSTCOPY          # trace copybook blast radius
python risk_score.py --gate --report CUSTCOPY   # score and CI gate
```

If `CUSTCOPY` scores HIGH (61/100 — five files `COPY` it, one JCL job depends on those programs), the PR is blocked and the author receives a Markdown comment with the score, reason, and regression test list.

## Why the Approach Is Creative and Unique

Three differentiators separate Blast Radius from existing dependency scanners.

**Confidence tiers on every edge.** Each dependency record carries `CONFIRMED` (statically certain) or `NEEDS_REVIEW` (runtime-resolved or library-symbolic). Uncertain edges surface for engineer review rather than being silently dropped.

**JCL batch-job and dataset tracing.** Most COBOL analysis stops at the source layer. Blast Radius traces through JCL job streams, `EXEC PGM=` steps, and dataset I/O, so a copybook change can be followed to which overnight batch jobs are affected.

**A hard CI gate.** The risk scorer exits with code 1 on HIGH, failing the GitHub Actions check and blocking merge. Dependency knowledge becomes enforced policy.
