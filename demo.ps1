# demo.ps1 - end-to-end Blast Radius pipeline demo
# Entity: CUSTCOPY (copybook) - scores HIGH

$ErrorActionPreference = 'Stop'

$Copybook       = "sample-cobol/COPYBOOK/CUSTCOPY.cpy"
$Entity         = "CUSTCOPY"
$HoursPerTest   = 2          # effort estimate: hours per regression test

# ==============================================================================
# STEP 1 - Show the copybook being changed
# ==============================================================================
Write-Host ""
Write-Host "======================================================================"
Write-Host "  STEP 1 - Simulated copybook change"
Write-Host "======================================================================"
Write-Host ""
Write-Host "  File: $Copybook"
Write-Host ""
Write-Host "  Before:"
Write-Host "    05 CUST-ACCT-BALANCE        PIC 9(7)V99."
Write-Host ""
Write-Host "  After (field widened to accommodate new currency precision):"
Write-Host "    05 CUST-ACCT-BALANCE        PIC 9(9)V99."
Write-Host ""
Write-Host "  A structural copybook change like this touches every program"
Write-Host "  that COPYs CUSTCOPY -- we need to know the full blast radius."
Write-Host ""

# ==============================================================================
# STEP 2 - Blast radius analysis
# ==============================================================================
Write-Host "======================================================================"
Write-Host "  STEP 2 - Blast radius analysis (blast_radius.py --changed)"
Write-Host "======================================================================"
Write-Host ""
python blast_radius.py --changed $Copybook
Write-Host ""

# ==============================================================================
# STEP 3 - Risk gate
# ==============================================================================
Write-Host "======================================================================"
Write-Host "  STEP 3 - Risk gate (risk_score.py --gate)"
Write-Host "======================================================================"
Write-Host ""
Write-Host "  Running: python risk_score.py --gate $Entity"
Write-Host ""

# Capture exit code without letting $ErrorActionPreference throw on non-zero
$ErrorActionPreference = 'Continue'
python risk_score.py --gate $Entity
$GateExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

if ($GateExit -eq 0) {
    Write-Host "  [PASS] Gate passed - band is not HIGH."
} else {
    Write-Host "  [BLOCKED] Band is HIGH. Pipeline would stop here in CI."
}
Write-Host ""

# ==============================================================================
# STEP 4 - Effort estimate
# ==============================================================================
Write-Host "======================================================================"
Write-Host "  STEP 4 - Effort estimate"
Write-Host "======================================================================"
Write-Host ""

# Capture regression-test list from risk_score.py (JSON mode)
$RiskJson  = python risk_score.py --json $Entity | Out-String
$RiskData  = $RiskJson | ConvertFrom-Json

$Band        = $RiskData.band
$Score       = $RiskData.score
$Tests       = $RiskData.regression_tests
$NTests      = $Tests.Count
$TotalHours  = $NTests * $HoursPerTest

Write-Host "  Entity  : $Entity"
Write-Host "  Score   : $Score / 100  ($Band)"
Write-Host ""
Write-Host "  Regression test suite:"
foreach ($t in $Tests) {
    Write-Host "    - $t"
}
Write-Host ""
Write-Host "  --- Estimate ----------------------------------------------------"
Write-Host "  Programs to retest  : $NTests"
Write-Host "  Assumed effort/test : $HoursPerTest hours"
Write-Host "  Total effort        : $TotalHours hours"
Write-Host "  -----------------------------------------------------------------"
Write-Host ""
Write-Host "  Review the blast radius output above to determine which JCL"
Write-Host "  jobs and downstream datasets are also at risk before merging."
Write-Host ""
