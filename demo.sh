#!/usr/bin/env bash
# demo.sh — end-to-end Blast Radius pipeline demo
# Entity: CUSTCOPY (copybook) — scores HIGH
set -euo pipefail

COPYBOOK="sample-cobol/COPYBOOK/CUSTCOPY.cpy"
ENTITY="CUSTCOPY"
HOURS_PER_TEST=2          # effort estimate: hours per regression test

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Show the copybook being changed
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  STEP 1 · Simulated copybook change                             ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  File: $COPYBOOK"
echo ""
echo "  Before:"
echo "    05 CUST-ACCT-BALANCE        PIC 9(7)V99."
echo ""
echo "  After (field widened to accommodate new currency precision):"
echo "    05 CUST-ACCT-BALANCE        PIC 9(9)V99."
echo ""
echo "  → A structural copybook change like this touches every program"
echo "    that COPYs CUSTCOPY — we need to know the full blast radius."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Blast radius analysis
# ─────────────────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  STEP 2 · Blast radius analysis (blast_radius.py --changed)     ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
python blast_radius.py --changed "$COPYBOOK"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Risk gate
# ─────────────────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  STEP 3 · Risk gate (risk_score.py --gate)                      ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Running: python risk_score.py --gate $ENTITY"
echo ""
set +e
python risk_score.py --gate "$ENTITY"
GATE_EXIT=$?
set -e
if [ "$GATE_EXIT" -eq 0 ]; then
    echo "  ✔  Gate passed — band is not HIGH."
else
    echo "  ✖  Gate BLOCKED — band is HIGH. Pipeline would stop here in CI."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Effort estimate
# ─────────────────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  STEP 4 · Effort estimate                                       ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Capture regression-test list from risk_score.py (JSON mode)
RISK_JSON=$(python risk_score.py --json "$ENTITY")
BAND=$(echo "$RISK_JSON"        | python -c "import sys,json; print(json.load(sys.stdin)['band'])")
SCORE=$(echo "$RISK_JSON"       | python -c "import sys,json; print(json.load(sys.stdin)['score'])")
TESTS=$(echo "$RISK_JSON"       | python -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(d['regression_tests']))")
N_TESTS=$(echo "$RISK_JSON"     | python -c "import sys,json; print(len(json.load(sys.stdin)['regression_tests']))")
TOTAL_HOURS=$(( N_TESTS * HOURS_PER_TEST ))

echo "  Entity  : $ENTITY"
echo "  Score   : $SCORE / 100  ($BAND)"
echo ""
echo "  Regression test suite:"
while IFS= read -r t; do
    echo "    • $t"
done <<< "$TESTS"
echo ""
echo "  ── Estimate ─────────────────────────────────────────────────"
echo "  Programs to retest  : $N_TESTS"
echo "  Assumed effort/test : $HOURS_PER_TEST hours"
echo "  Total effort        : $TOTAL_HOURS hours"
echo "  ─────────────────────────────────────────────────────────────"
echo ""
echo "  Review the blast radius output above to determine which JCL"
echo "  jobs and downstream datasets are also at risk before merging."
echo ""
