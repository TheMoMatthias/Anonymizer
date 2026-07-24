param(
    # Install the optional ML detection stack (GLiNER + onnxruntime) into the
    # bundle and vendor the model from vendor\gliner-model. Off by default so the
    # base bundle stays lean; pass -WithML on a CONNECTED build machine once the
    # model is prepared (see docs/run_gliner-integration_2026-07-24.md runbook).
    [switch]$WithML
)
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

Write-Host "Installing the app, its dependencies, and both spaCy models into the bundled runtime..."
# This runtime exists solely to be this bundle's isolated environment (it's
# never used as anyone's "system Python"), so overriding uv's externally-
# managed guard here is intentional, not a safety bypass of a shared install.
# The spaCy models are direct-URL dependencies in pyproject.toml, so this single
# install pulls them in too -- no separate `spacy download` (which would need
# pip in the relocatable runtime).
$installTarget = "$repoRoot"
if ($WithML) {
    Write-Host "  -WithML: also installing the optional ML detection stack (gliner + onnxruntime)."
    $installTarget = "$repoRoot[ml]"
}
uv pip install --python $pythonExe --break-system-packages $installTarget
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

Write-Host "Copying launcher, installer, and FAQ..."
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\launch.bat") $bundleDir
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\install.ps1") $bundleDir
Copy-Item (Join-Path $PSScriptRoot "bundle_templates\FAQ.md") $bundleDir

Write-Host "Setting up the OCR (Tesseract) drop-in folder..."
# OCR for scanned PDFs uses a PORTABLE Tesseract dropped into <bundle>\tesseract\.
# If the repo vendors one at vendor\tesseract, ship it; otherwise leave the
# folder with instructions so it can be added without a rebuild.
$tessTarget = Join-Path $bundleDir "tesseract"
$tessVendor = Join-Path $repoRoot "vendor\tesseract"
if (Test-Path $tessVendor) {
    Copy-Item $tessVendor $tessTarget -Recurse
    Write-Host "  Bundled portable Tesseract from vendor\tesseract."
} else {
    New-Item -ItemType Directory -Force -Path $tessTarget | Out-Null
    @"
Drop a PORTABLE Tesseract-OCR here to enable OCR of scanned/image PDFs.

Required layout (no installer, no admin rights):
  tesseract\tesseract.exe
  tesseract\tessdata\deu.traineddata
  tesseract\tessdata\eng.traineddata

A portable build (e.g. the UB Mannheim Windows build, or the contents of an
existing install's Tesseract-OCR folder) works. The launcher auto-detects
tesseract.exe here and turns OCR on. Without it, scanned PDFs are refused
(never silently passed through).
"@ | Set-Content -Path (Join-Path $tessTarget "README.txt") -Encoding UTF8
    Write-Host "  No vendor\tesseract found; wrote drop-in instructions."
}

Write-Host "Setting up the GLiNER (AI detection) model drop-in folder..."
# AI detection uses an OFFLINE GLiNER model dropped into <bundle>\gliner-model\.
# The launcher sets ANONYMIZER_GLINER_MODEL to it, so an enabled model loads with
# no download. If the repo vendors one at vendor\gliner-model, ship it; otherwise
# leave the folder with instructions so it can be added without a rebuild.
$glinerTarget = Join-Path $bundleDir "gliner-model"
$glinerVendor = Join-Path $repoRoot "vendor\gliner-model"
if (Test-Path $glinerVendor) {
    Copy-Item $glinerVendor $glinerTarget -Recurse
    Write-Host "  Bundled GLiNER model from vendor\gliner-model."
} else {
    New-Item -ItemType Directory -Force -Path $glinerTarget | Out-Null
    @"
Drop an OFFLINE GLiNER model here to enable AI detection (Settings -> AI detection).

Expected layout (a from_pretrained snapshot with an ONNX build):
  gliner-model\gliner_config.json
  gliner-model\onnx\model.onnx        (int8-quantised recommended)
  gliner-model\tokenizer.json  (+ tokenizer/model support files)

Prepare it on a CONNECTED machine per the runbook in
docs\run_gliner-integration_2026-07-24.md ("Connected-machine runbook"): fetch
gliner_multi-v2.1, export/quantise to ONNX, and copy the snapshot folder here.
The launcher auto-detects this folder and sets ANONYMIZER_GLINER_MODEL. Without
it, AI detection stays off (the rule-based + spaCy pass still runs); enabling it
in Settings without a model here makes scanning stop with a clear error.
"@ | Set-Content -Path (Join-Path $glinerTarget "README.txt") -Encoding UTF8
    Write-Host "  No vendor\gliner-model found; wrote drop-in instructions."
}

$sizeBytes = (Get-ChildItem -Path $bundleDir -Recurse | Measure-Object -Property Length -Sum).Sum
$sizeMB = [math]::Round($sizeBytes / 1MB, 1)
Write-Host ""
Write-Host "Done. Offline bundle ready at: $bundleDir ($sizeMB MB)"
Write-Host "Copy this whole folder to the internal share for distribution."
