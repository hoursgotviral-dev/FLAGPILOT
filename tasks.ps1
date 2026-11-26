param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "install", "test", "lint", "demo-plan", "demo-apply", "demo-run", "demo-rollback", "demo-report", "demo")]
    [string]$Task = "help"
)

$ErrorActionPreference = "Stop"

$Python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$FlagPilot = if (Test-Path ".venv\Scripts\flagpilot.exe") { ".venv\Scripts\flagpilot.exe" } else { "flagpilot" }
$Pytest = if (Test-Path ".venv\Scripts\pytest.exe") { ".venv\Scripts\pytest.exe" } else { "pytest" }
$Ruff = if (Test-Path ".venv\Scripts\ruff.exe") { ".venv\Scripts\ruff.exe" } else { "ruff" }

switch ($Task) {
    "help" {
        Write-Host "Available tasks: install, test, lint, demo-plan, demo-apply, demo-run, demo-rollback, demo-report, demo"
    }
    "install" {
        python -m venv .venv
        & $Python -m pip install -e ".[dev]"
    }
    "test" {
        & $Pytest -v
    }
    "lint" {
        & $Ruff check src tests
    }
    "demo-plan" {
        Write-Host "=== [1/5] PLAN (offline, no Flagsmith calls) ==="
        & $FlagPilot plan "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"
    }
    "demo-apply" {
        Write-Host "=== [2/5] APPLY (HUMAN ACTION: type APPROVE when prompted) ==="
        & $FlagPilot apply plan.json
    }
    "demo-run" {
        Write-Host "=== [3/5] RUN WITH SEEDED ERROR SPIKE AT 50% (HUMAN ACTION: type APPROVE at each step & rollback) ==="
        & $FlagPilot run `
            --feature checkout_v2 `
            --env Development `
            --stages 1 10 50 100 `
            --max-error-rate 0.02 `
            --baseline-error-rate 0.005 `
            --spike-rate 0.25 `
            --spike-threshold-pct 25 `
            --seed 42 `
            --interval 0
    }
    "demo-rollback" {
        Write-Host "=== [4/5] ROLLBACK TO 0% (HUMAN ACTION: type APPROVE when prompted) ==="
        & $FlagPilot rollback `
            --feature checkout_v2 `
            --env Development `
            --old-percentage 10 `
            --reason "demo cleanup"
    }
    "demo-report" {
        Write-Host "=== [5/5] REPORT (static reports/rollout.html from audit.log + reports/last_run.json) ==="
        & $FlagPilot report --input reports/last_run.json --audit-log audit.log --out reports/rollout.html
    }
    "demo" {
        & $MyInvocation.MyCommand.Path demo-plan
        & $MyInvocation.MyCommand.Path demo-apply
        try {
            & $MyInvocation.MyCommand.Path demo-run
        } catch {
            Write-Host "Run exited with non-zero status (expected when spike triggers rollback)."
        }
        & $MyInvocation.MyCommand.Path demo-rollback
        & $MyInvocation.MyCommand.Path demo-report
        Write-Host "Demo complete. Open reports/rollout.html in a browser."
    }
}

