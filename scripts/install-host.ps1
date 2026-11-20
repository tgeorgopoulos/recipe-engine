<#
.SYNOPSIS
  Install (or update) the Recipe Engine host on this Windows PC and make sure it is running.

.DESCRIPTION
  Idempotent. Run by the Claude Code plugin's SessionStart hook and by hand:
      powershell -ExecutionPolicy Bypass -File scripts\install-host.ps1
  1. If %LOCALAPPDATA%\Programs\RecipeEngine\recipe-engine.exe is missing or older than
     $Version: download recipe-engine-win64.zip + recipe-engine-win64.zip.sha256 from the
     GitHub release, verify the SHA-256 (the download is discarded on mismatch), unzip.
  2. `recipe-engine.exe install`: data dir, per-install token (ACL: you only), HKCU Run key.
  3. Start the host if /api/health does not answer, and print where the token lives.
  No admin rights, nothing outside your user profile. -Quiet keeps the hook output short.
#>
[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [string]$Repo = "tgeorgopoulos/recipe-engine",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\RecipeEngine"),
    [string]$ZipUrl = "",
    [string]$Sha256Url = "",
    [switch]$Quiet,
    [switch]$NoStart
)
$ErrorActionPreference = "Stop"
function Say($m) { if (-not $Quiet) { Write-Host $m } }

$exe = Join-Path $InstallDir "recipe-engine.exe"
$exew = Join-Path $InstallDir "recipe-enginew.exe"
$tokenFile = Join-Path $env:LOCALAPPDATA "RecipeEngine\token"
$healthUrl = "http://127.0.0.1:47391/api/health"

function Installed-Version {
    if (-not (Test-Path $exe)) { return "" }
    try { return ((& $exe --version) 2>$null | Select-Object -First 1).Trim() } catch { return "" }
}

$have = Installed-Version
if ($have -ne $Version) {
    if (-not $ZipUrl) { $ZipUrl = "https://github.com/$Repo/releases/download/v$Version/recipe-engine-win64.zip" }
    if (-not $Sha256Url) { $Sha256Url = "$ZipUrl.sha256" }
    $tmp = Join-Path $env:TEMP ("recipe-engine-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zip = Join-Path $tmp "recipe-engine-win64.zip"
    Say "downloading Recipe Engine $Version ($ZipUrl)"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing
    $expected = ((Invoke-WebRequest -Uri $Sha256Url -UseBasicParsing).Content -split '\s+')[0].Trim().ToLower()
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($expected.Length -ne 64 -or $actual -ne $expected) {
        Remove-Item $tmp -Recurse -Force
        throw "SHA-256 mismatch for $ZipUrl (expected $expected, got $actual) - download discarded"
    }
    Say "sha256 ok ($actual)"
    # stop a running host before replacing its files
    try { Get-Process recipe-engine, recipe-enginew -ErrorAction SilentlyContinue | Stop-Process -Force } catch {}
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "recipe-engine.exe") } | Select-Object -First 1
    $srcDir = if ($inner) { $inner.FullName } else { $tmp }
    Get-ChildItem $InstallDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Copy-Item (Join-Path $srcDir "*") $InstallDir -Recurse -Force
    Remove-Item $tmp -Recurse -Force
    Say "installed to $InstallDir"
} else {
    Say "Recipe Engine $Version already installed at $InstallDir"
}

& $exe install | ForEach-Object { Say $_ }
if ($LASTEXITCODE -ne 0) { throw "recipe-engine install failed ($LASTEXITCODE)" }

if (-not $NoStart) {
    $up = $false
    try { $up = (Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
    if (-not $up) {
        Say "starting the host"
        Start-Process -FilePath $exew -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 40 -and -not $up; $i++) {
            Start-Sleep -Milliseconds 250
            try { $up = (Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
        }
    }
    if ($up) { Say "host is up: http://127.0.0.1:47391  (token: $tokenFile)" } else { Write-Warning "host did not answer on $healthUrl" }
}
Say "plugin setup: paste the contents of $tokenFile into the plugin's 'Engine token' option (recipe-engine token prints it)."
