# Build dist/recipe-engine.mcpb from mcpb/ (manifest.json + server/shim.js, no dependencies).
#   powershell -ExecutionPolicy Bypass -File mcpb\build.ps1
# Requires node + npm (npx @anthropic-ai/mcpb). Validates the manifest first; fails loudly.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "mcpb"
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force $dist | Out-Null
$out = Join-Path $dist "recipe-engine.mcpb"
if (Test-Path $out) { Remove-Item $out -Force }

Push-Location $src
try {
    npx --yes @anthropic-ai/mcpb validate manifest.json
    if ($LASTEXITCODE -ne 0) { throw "mcpb validate failed" }
    npx --yes @anthropic-ai/mcpb pack . $out
    if ($LASTEXITCODE -ne 0) { throw "mcpb pack failed" }
} finally { Pop-Location }
$hash = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
"$hash  recipe-engine.mcpb" | Out-File -Encoding ascii "$out.sha256"
Write-Host "built $out ($hash)"
