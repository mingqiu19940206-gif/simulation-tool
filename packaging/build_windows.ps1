[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonCandidate = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
if (Test-Path -LiteralPath $PythonCandidate) {
    $Python = $PythonCandidate
} else {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $Command) { throw "64-bit CPython 3.11-3.14 is required." }
    $Python = $Command.Source
}

$Version = (Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "VERSION")).Trim()
if ($Version -ne "2.4.0") { throw "VERSION must be the single delivery version 2.4.0." }
$ReleaseDirectory = Join-Path $ProjectRoot "release"
$ExpectedExe = Join-Path $ReleaseDirectory "SurfaceCapAntipadTool_v2.4.0.exe"
$TemporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("SurfaceCapAntipadTool_v2.4.0_build_" + [Guid]::NewGuid().ToString("N"))
$WorkPath = Join-Path $TemporaryRoot "pyinstaller"
$MplPath = Join-Path $TemporaryRoot "matplotlib"
$SelfCheckPath = Join-Path $TemporaryRoot "release_self_check.json"
$PreviewMarker = Join-Path $ProjectRoot "verification\exe_model_preview_smoke.json"

New-Item -ItemType Directory -Path $TemporaryRoot,$WorkPath,$MplPath,$ReleaseDirectory -Force | Out-Null
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$env:MPLCONFIGDIR = $MplPath

try {
    $RuntimeFiles = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "src") -File -Recurse -Filter "*.py" | Sort-Object FullName
    $RuntimeFiles += Get-Item -LiteralPath (Join-Path $ProjectRoot "config\default_config.json")
    $SourceHashLines = foreach ($File in $RuntimeFiles) {
        if (-not $File.FullName.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Source file is outside the project root: $($File.FullName)"
        }
        $Relative = $File.FullName.Substring($ProjectRoot.Length).TrimStart("\").Replace("\", "/")
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $File.FullName).Hash.ToLowerInvariant()
        "$Relative=$Hash"
    }
    $Sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes(($SourceHashLines -join "`n"))
        $SourceHash = ([System.BitConverter]::ToString($Sha.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $Sha.Dispose()
    }
    $Metadata = [ordered]@{
        software_version = $Version
        build_time_utc = [DateTime]::UtcNow.ToString("o")
        source_hash = $SourceHash
        sweep_backend_version = "native-optimetrics-v3.2"
        config_schema_version = 3
        source_files = @($RuntimeFiles | ForEach-Object { $_.FullName.Substring($ProjectRoot.Length).TrimStart("\").Replace("\", "/") })
    }
    $Metadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ProjectRoot "config\build_metadata.json") -Encoding UTF8

    Push-Location $ProjectRoot
    try {
        & $Python "tools\run_tests.py" "verification\test_results.json"
        if ($LASTEXITCODE -ne 0) { throw "Automated tests failed; packaging stopped." }
        & $Python "tools\verify_delivery.py" "verification\source_verification.json"
        if ($LASTEXITCODE -ne 0) { throw "Source delivery verification failed; packaging stopped." }
        & $Python "src\main_gui.py" --smoke-test
        if ($LASTEXITCODE -ne 0) { throw "Source GUI smoke test failed; packaging stopped." }
        & $Python "src\main_gui.py" --model-preview-smoke-test
        if ($LASTEXITCODE -ne 0) { throw "Source 3D interaction smoke test failed; packaging stopped." }
        & $Python -m PyInstaller --noconfirm --clean --workpath $WorkPath --distpath $ReleaseDirectory "packaging\SurfaceCapAntipadTool.spec"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $ExpectedExe)) { throw "Expected EXE was not generated: $ExpectedExe" }
    $SelfCheck = Start-Process -FilePath $ExpectedExe -ArgumentList @("--release-self-check", $SelfCheckPath) -Wait -PassThru -WindowStyle Hidden
    if ($SelfCheck.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SelfCheckPath)) { throw "EXE release self-check failed." }
    $SelfCheckPayload = Get-Content -Raw -Encoding UTF8 -LiteralPath $SelfCheckPath | ConvertFrom-Json
    if (-not $SelfCheckPayload.passed -or $SelfCheckPayload.software_version -ne $Version -or $SelfCheckPayload.source_hash -ne $SourceHash) {
        throw "EXE build identity or native Optimetrics self-check mismatch."
    }
    $GuiSmoke = Start-Process -FilePath $ExpectedExe -ArgumentList @("--smoke-test") -Wait -PassThru -WindowStyle Hidden
    if ($GuiSmoke.ExitCode -ne 0) { throw "EXE GUI startup smoke test failed." }
    $PreviewSmoke = Start-Process -FilePath $ExpectedExe -ArgumentList @("--model-preview-smoke-test") -Wait -PassThru -WindowStyle Hidden
    if ($PreviewSmoke.ExitCode -ne 0) { throw "EXE 3D rotate/pan/zoom/Validate smoke test failed." }
    [ordered]@{
        passed = $true
        executable = "release/SurfaceCapAntipadTool_v2.4.0.exe"
        software_version = $Version
        source_hash = $SourceHash
        release_self_check = $SelfCheckPayload
        gui_startup_exit_code = $GuiSmoke.ExitCode
        model_preview_exit_code = $PreviewSmoke.ExitCode
        verified_at_utc = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $PreviewMarker -Encoding UTF8
    Get-FileHash -Algorithm SHA256 -LiteralPath $ExpectedExe | Format-List
} finally {
    if (Test-Path -LiteralPath $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
    }
}
