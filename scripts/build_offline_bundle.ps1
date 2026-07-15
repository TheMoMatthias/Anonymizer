$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$bundleDir = Join-Path $repoRoot "dist\Anonymizer-offline"

if (Test-Path $bundleDir) {
    Remove-Item $bundleDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $bundleDir | Out-Null

Write-Host "Installing a relocatable Python runtime into the bundle..."
uv python install --install-dir "$bundleDir\python" 3.12
if ($LASTEXITCODE -ne 0) { throw "uv python install failed" }

$pythonExe = Get-ChildItem -Path "$bundleDir\python" -Filter "python.exe" -Recurse | Select-Object -First 1 -ExpandProperty FullName
$runtimeDir = Split-Path -Parent $pythonExe
$normalizedDir = Join-Path $bundleDir "python\runtime"
if ($runtimeDir -ne $normalizedDir) {
    Move-Item $runtimeDir $normalizedDir
    $pythonExe = Join-Path $normalizedDir "python.exe"
}

Write-Host "Installing the app and its dependencies into the bundled runtime..."
# This runtime exists solely to be this bundle's isolated environment (it's
# never used as anyone's "system Python"), so overriding uv's externally-
# managed guard here is intentional, not a safety bypass of a shared install.
uv pip install --python $pythonExe --break-system-packages "$repoRoot"
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

Write-Host "Downloading spaCy language models into the bundled runtime..."
# spacy download shells out to this runtime's own pip internally, which
# would hit the same externally-managed guard as above.
$env:PIP_BREAK_SYSTEM_PACKAGES = "1"
& $pythonExe -m spacy download de_core_news_md
if ($LASTEXITCODE -ne 0) { throw "spacy download de_core_news_md failed" }
& $pythonExe -m spacy download en_core_web_md
if ($LASTEXITCODE -ne 0) { throw "spacy download en_core_web_md failed" }

Write-Host "Copying launcher, installer, and FAQ..."
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\launch.bat") $bundleDir
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\install.ps1") $bundleDir
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\FAQ.md") $bundleDir

$sizeBytes = (Get-ChildItem -Path $bundleDir -Recurse | Measure-Object -Property Length -Sum).Sum
$sizeMB = [math]::Round($sizeBytes / 1MB, 1)
Write-Host ""
Write-Host "Done. Offline bundle ready at: $bundleDir ($sizeMB MB)"
Write-Host "Copy this whole folder to the internal share for distribution."
