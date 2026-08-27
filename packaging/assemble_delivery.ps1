[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$SourceRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$DeliveryRoot = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $SourceRoot) "delivery"))
$LeafName = "SurfaceCapAntipadTool_v2.4.0_latest"
$Target = [System.IO.Path]::GetFullPath((Join-Path $DeliveryRoot $LeafName))
$ZipPath = [System.IO.Path]::GetFullPath((Join-Path $DeliveryRoot ($LeafName + "_full.zip")))
$ZipHashPath = $ZipPath + ".sha256"
$Staging = [System.IO.Path]::GetFullPath((Join-Path $DeliveryRoot ("." + $LeafName + "_staging_" + [Guid]::NewGuid().ToString("N"))))
$ExpectedExeRelative = "release\SurfaceCapAntipadTool_v2.4.0.exe"

if ((Get-Content -Raw -LiteralPath (Join-Path $SourceRoot "VERSION")).Trim() -ne "2.4.0") {
    throw "Only VERSION 2.4.0 may be assembled by this release script."
}
$RequiredPrefix = $DeliveryRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
foreach ($Candidate in @($Target, $Staging)) {
    if (-not $Candidate.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside the delivery root: $Candidate"
    }
}
if ((Split-Path -Leaf $Target) -ne $LeafName) {
    throw "Unexpected delivery target: $Target"
}

New-Item -ItemType Directory -Force -Path $DeliveryRoot,$Staging | Out-Null
try {
    & robocopy.exe $SourceRoot $Staging /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP `
        /XD __pycache__ .pytest_cache build .git `
        /XF *.pyc *.pyo FILE_MANIFEST.txt SHA256SUMS.txt "test_results_prebuild*.json" `
            SurfaceCapAntipadTool_v2.3.1.exe
    if ($LASTEXITCODE -gt 7) { throw "robocopy failed with exit code $LASTEXITCODE" }

    $ExpectedExe = Join-Path $Staging $ExpectedExeRelative
    if (-not (Test-Path -LiteralPath $ExpectedExe)) { throw "Missing release EXE: $ExpectedExe" }

    $ManifestFiles = Get-ChildItem -LiteralPath $Staging -File -Recurse | Sort-Object FullName
    $ManifestLines = @(
        "SurfaceCapAntipadTool 2.4.0 complete SHA-256 manifest"
        "Generated: $([DateTime]::UtcNow.ToString('o'))"
        "SHA256`tBytes`tPath"
    )
    foreach ($File in $ManifestFiles) {
        $Relative = $File.FullName.Substring($Staging.Length).TrimStart("\").Replace("\", "/")
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $File.FullName).Hash.ToLowerInvariant()
        $ManifestLines += "$Hash`t$($File.Length)`t$Relative"
    }
    $ManifestLines | Set-Content -LiteralPath (Join-Path $Staging "FILE_MANIFEST.txt") -Encoding UTF8

    $ChecksumRelative = @(
        "VERSION",
        $ExpectedExeRelative,
        "config\default_config.json",
        "config\build_metadata.json",
        "packaging\build_windows.ps1",
        "packaging\SurfaceCapAntipadTool.spec",
        "packaging\version_info.txt",
        "verification\test_results.json",
        "verification\semantic_invariance.json",
        "verification\visual_acceptance.json",
        "FILE_MANIFEST.txt"
    )
    $ChecksumLines = foreach ($Relative in $ChecksumRelative) {
        $Path = Join-Path $Staging $Relative
        if (-not (Test-Path -LiteralPath $Path)) { throw "Missing checksum target: $Relative" }
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
        "$Hash  $($Relative.Replace('\', '/'))"
    }
    $ChecksumLines | Set-Content -LiteralPath (Join-Path $Staging "SHA256SUMS.txt") -Encoding UTF8

    if (Test-Path -LiteralPath $Target) {
        $ResolvedTarget = [System.IO.Path]::GetFullPath($Target)
        if ($ResolvedTarget -ne $Target -or (Split-Path -Leaf $ResolvedTarget) -ne $LeafName) {
            throw "Refusing to replace an unexpected directory: $ResolvedTarget"
        }
        Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
    }
    Move-Item -LiteralPath $Staging -Destination $Target

    foreach ($OldFile in @($ZipPath, $ZipHashPath)) {
        if (Test-Path -LiteralPath $OldFile) { Remove-Item -LiteralPath $OldFile -Force }
    }
    Compress-Archive -LiteralPath $Target -DestinationPath $ZipPath -CompressionLevel Optimal
    $ZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
    "$ZipHash  $(Split-Path -Leaf $ZipPath)" | Set-Content -LiteralPath $ZipHashPath -Encoding ASCII
    $ExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Target $ExpectedExeRelative)).Hash.ToLowerInvariant()
    [ordered]@{
        passed = $true
        directory = $Target
        file_count = (Get-ChildItem -LiteralPath $Target -File -Recurse).Count
        executable = (Join-Path $Target $ExpectedExeRelative)
        executable_sha256 = $ExeHash
        zip = $ZipPath
        zip_sha256 = $ZipHash
        zip_hash_file = $ZipHashPath
    } | ConvertTo-Json -Depth 3
} finally {
    if (Test-Path -LiteralPath $Staging) {
        $ResolvedStaging = [System.IO.Path]::GetFullPath($Staging)
        if ($ResolvedStaging.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $ResolvedStaging).StartsWith("." + $LeafName + "_staging_")) {
            Remove-Item -LiteralPath $ResolvedStaging -Recurse -Force
        }
    }
}
