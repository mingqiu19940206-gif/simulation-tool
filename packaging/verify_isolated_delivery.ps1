[CmdletBinding()]
param(
    [string]$ZipPath
)

$ErrorActionPreference = "Stop"
$SourceRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$LeafName = "SurfaceCapAntipadTool_v2.4.0_latest"
if (-not $ZipPath) {
    $ZipPath = Join-Path (Join-Path (Split-Path -Parent $SourceRoot) "delivery") ($LeafName + "_full.zip")
}
$ZipPath = [System.IO.Path]::GetFullPath($ZipPath)
if (-not (Test-Path -LiteralPath $ZipPath)) { throw "Delivery ZIP not found: $ZipPath" }
$PythonCandidate = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
if (Test-Path -LiteralPath $PythonCandidate) { $Python = $PythonCandidate } else { $Python = (Get-Command python.exe).Source }

$TempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$TempRoot = [System.IO.Path]::GetFullPath((Join-Path $TempBase ("SurfaceCapAntipadTool_v2.4.0_isolation_" + [Guid]::NewGuid().ToString("N"))))
$RequiredPrefix = $TempBase.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $TempRoot.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing isolation path outside the system temp directory: $TempRoot"
}

New-Item -ItemType Directory -Path $TempRoot | Out-Null
try {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $TempRoot -Force
    $ProjectRoot = Join-Path $TempRoot $LeafName
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "VERSION"))) {
        throw "ZIP did not contain the expected top-level directory: $LeafName"
    }
    if ((Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "VERSION")).Trim() -ne "2.4.0") {
        throw "Isolated VERSION mismatch."
    }
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    Push-Location $ProjectRoot
    try {
        & $Python "tools\run_tests.py" "verification\isolation_test_results.json"
        if ($LASTEXITCODE -ne 0) { throw "Isolated full test suite failed." }
        & $Python "tools\verify_delivery.py" "verification\isolation_source_verification.json"
        if ($LASTEXITCODE -ne 0) { throw "Isolated source verification failed." }

        $Exe = Join-Path $ProjectRoot "release\SurfaceCapAntipadTool_v2.4.0.exe"
        $SelfCheckPath = Join-Path $ProjectRoot "verification\isolation_exe_self_check.json"
        $SelfCheck = Start-Process -FilePath $Exe -ArgumentList @("--release-self-check", $SelfCheckPath) -Wait -PassThru -WindowStyle Hidden
        if ($SelfCheck.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SelfCheckPath)) {
            throw "Isolated EXE release self-check failed."
        }
        $GuiSmoke = Start-Process -FilePath $Exe -ArgumentList @("--smoke-test") -Wait -PassThru -WindowStyle Hidden
        if ($GuiSmoke.ExitCode -ne 0) { throw "Isolated EXE GUI smoke failed." }
        $ModelSmoke = Start-Process -FilePath $Exe -ArgumentList @("--model-preview-smoke-test") -Wait -PassThru -WindowStyle Hidden
        if ($ModelSmoke.ExitCode -ne 0) { throw "Isolated EXE model-preview smoke failed." }

        $Tests = Get-Content -Raw -Encoding UTF8 -LiteralPath "verification\isolation_test_results.json" | ConvertFrom-Json
        $SourceVerification = Get-Content -Raw -Encoding UTF8 -LiteralPath "verification\isolation_source_verification.json" | ConvertFrom-Json
        $SelfCheckPayload = Get-Content -Raw -Encoding UTF8 -LiteralPath $SelfCheckPath | ConvertFrom-Json
        $Summary = [ordered]@{
            passed = $true
            tested_candidate_zip = $ZipPath
            tested_candidate_zip_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
            isolated_directory = $TempRoot
            tests_run = $Tests.tests_run
            test_failures = $Tests.failures
            test_errors = $Tests.errors
            source_verification_passed = $SourceVerification.passed
            exe_self_check_passed = $SelfCheckPayload.passed
            gui_smoke_exit_code = $GuiSmoke.ExitCode
            model_preview_smoke_exit_code = $ModelSmoke.ExitCode
            verified_at_utc = [DateTime]::UtcNow.ToString("o")
            temporary_copy_removed_after_verification = $true
        }
        $Summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "verification\isolation_summary.json" -Encoding UTF8
    } finally {
        Pop-Location
    }

    foreach ($Name in @(
        "isolation_test_results.json",
        "isolation_source_verification.json",
        "isolation_exe_self_check.json",
        "isolation_summary.json"
    )) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot ("verification\" + $Name)) `
            -Destination (Join-Path $SourceRoot ("verification\" + $Name)) -Force
    }
    Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $SourceRoot "verification\isolation_summary.json")
} finally {
    if (Test-Path -LiteralPath $TempRoot) {
        $Resolved = [System.IO.Path]::GetFullPath($TempRoot)
        if ($Resolved.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $Resolved).StartsWith("SurfaceCapAntipadTool_v2.4.0_isolation_")) {
            Remove-Item -LiteralPath $Resolved -Recurse -Force
        }
    }
}
