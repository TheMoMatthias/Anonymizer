# Builds the two double-clickable launchers, Anonymizer.exe and Install.exe,
# into the repository root. They are native twins of Anonymizer.bat and
# Install.bat -- self-contained, so they do not shell out to the .bat files.
#
# Compiled with the .NET Framework 4 C# compiler that ships inside Windows
# itself: no SDK, no toolchain install, and the resulting ~10 KB executables
# need nothing installed on the target machine (.NET Framework 4.x is a
# built-in Windows component on Windows 8 and later).
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\build_launchers.ps1
# The .exe files are committed, so this only needs re-running when the .cs
# sources change.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$srcDir   = Join-Path $PSScriptRoot 'launcher_src'
$manifest = Join-Path $srcDir 'launcher.manifest'
$shared   = Join-Path $srcDir 'Shared.cs'

# Prefer the 64-bit compiler; fall back to 32-bit on an x86-only Windows.
$cscCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$csc = $cscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) {
    throw "No .NET Framework 4 C# compiler found. Looked in: $($cscCandidates -join ', ')"
}
Write-Host "Compiler: $csc"

# Validate the manifest before compiling. csc embeds it verbatim and does not
# check it, so a malformed manifest produces two executables that Windows
# refuses to launch at all: "side-by-side configuration is incorrect", raised by
# the loader before any of our code runs, with the real reason buried in the
# Application event log. Both checks below have already caught a real breakage.
$manifestBytes = [System.IO.File]::ReadAllBytes($manifest)
if ($manifestBytes.Length -ge 3 -and
    $manifestBytes[0] -eq 0xEF -and $manifestBytes[1] -eq 0xBB -and $manifestBytes[2] -eq 0xBF) {
    throw "$manifest starts with a UTF-8 BOM. A BOM before the XML declaration stops the .exe from starting; re-save it BOM-less."
}
# Catches the other easy mistake: a double hyphen used as a dash inside an XML
# comment, which is illegal XML that only surfaces at process launch.
try { [xml](Get-Content $manifest -Raw) | Out-Null }
catch { throw "$manifest is not valid XML, so the built .exe would not start: $($_.Exception.Message)" }

# Each launcher is its own source file plus the shared helper. /platform:anycpu
# keeps one binary working on both x64 and arm64 Windows.
$targets = @(
    @{ Out = 'Anonymizer.exe'; Source = 'AnonymizerLauncher.cs' },
    @{ Out = 'Install.exe';    Source = 'InstallLauncher.cs'    }
)

foreach ($target in $targets) {
    $outPath = Join-Path $repoRoot $target.Out
    $srcPath = Join-Path $srcDir $target.Source

    # csc refuses to overwrite a running executable, and a stale .exe left in
    # place after a failed build is worse than no .exe at all.
    if (Test-Path $outPath) { Remove-Item $outPath -Force }

    Write-Host "Building $($target.Out) ..."
    & $csc `
        /nologo `
        /target:exe `
        /platform:anycpu `
        /optimize+ `
        /warnaserror `
        /win32manifest:"$manifest" `
        /out:"$outPath" `
        "$srcPath" "$shared"
    if ($LASTEXITCODE -ne 0) { throw "Build of $($target.Out) failed (csc exit $LASTEXITCODE)" }

    $sizeKb = [math]::Round((Get-Item $outPath).Length / 1KB, 1)
    Write-Host "  -> $outPath ($sizeKb KB)"
}

Write-Host ''
Write-Host 'Done. Both launchers are in the repository root.'
