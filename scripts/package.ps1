# Build the host bundle: dist/recipe-engine/ (PyInstaller) -> dist/recipe-engine-win64.zip + .sha256
#   powershell -ExecutionPolicy Bypass -File scripts\package.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
    & $py -m PyInstaller --noconfirm --clean recipe-engine.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
    $zip = Join-Path $root "dist\recipe-engine-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $root "dist\recipe-engine\*") -DestinationPath $zip
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    "$hash  recipe-engine-win64.zip" | Out-File -Encoding ascii "$zip.sha256"
    Write-Host "built $zip ($hash)"
} finally { Pop-Location }
