#Requires -Version 5.1
<#
.SYNOPSIS
    Validate a Windows radio-tools install end to end.

.DESCRIPTION
    One-shot check to run after windows\install.ps1. Prints PASS or FAIL for
    each item and exits non-zero if anything failed:

      * Python launcher 3.11 or newer
      * repo virtualenv (.venv\Scripts\python.exe)
      * broadcast + engine importable from that virtualenv
      * liquidsoap.exe resolvable by the engine
      * icecast.exe resolvable by the engine
      * Icecast web/admin share directories resolvable
      * secrets, config render and binaries validated by "radio start --dry-run"

    Nothing is started: the dry run spawns no processes.

.EXAMPLE
    .\windows\validate-windows.ps1
    $LASTEXITCODE
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$script:Failures = 0
$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Heading {
    param([string]$Text)
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Result {
    param(
        [bool]$Ok,
        [string]$Label,
        [string]$Detail
    )
    if ($Ok) {
        Write-Host "PASS  $Label" -ForegroundColor Green
    }
    else {
        Write-Host "FAIL  $Label" -ForegroundColor Red
        $script:Failures = $script:Failures + 1
    }
    if ($Detail) {
        Write-Host "      $Detail" -ForegroundColor DarkGray
    }
}

function Invoke-Capture {
    <#
        Run a command, capture stdout+stderr and the exit code.
        Returns a hashtable: Output, ExitCode, Started.
    #>
    param(
        [string]$FilePath,
        [string]$Arguments,
        [string]$WorkingDirectory
    )
    $result = @{ Output = ''; ExitCode = -1; Started = $false }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $FilePath
        $psi.Arguments = $Arguments
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        if ($WorkingDirectory) {
            $psi.WorkingDirectory = $WorkingDirectory
        }
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
        $result.Output = ($stdout + $stderr).Trim()
        $result.ExitCode = $proc.ExitCode
        $result.Started = $true
    }
    catch {
        $result.Output = $_.Exception.Message
    }
    return $result
}

function Test-MinimumVersion {
    param(
        [string]$VersionString,
        [version]$Minimum
    )
    $clean = ($VersionString -replace '^[^0-9]*', '') -split '\s+' | Select-Object -First 1
    [version]$parsed = $null
    if ([version]::TryParse($clean, [ref]$parsed)) {
        return ($parsed -ge $Minimum)
    }
    return $false
}

function Get-PathsValue {
    <#
        Pull "key: value" out of the "radio paths --show" output.
        Returns an empty string when the key is absent or "(not found)".
    #>
    param(
        [string]$Output,
        [string]$Key
    )
    foreach ($line in ($Output -split "`r?`n")) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("$Key" + ':')) {
            $value = $trimmed.Substring($Key.Length + 1).Trim()
            if ($value -eq '(not found)') {
                return ''
            }
            return $value
        }
    }
    return ''
}

Write-Host "radio-tools Windows validation" -ForegroundColor White
Write-Host "Repo root: $script:RepoRoot" -ForegroundColor DarkGray

# ── 1. Python launcher ─────────────────────────────────────────────────

Write-Heading 'Python'

$pythonOk = $false
$pythonDetail = 'no python 3.11+ found; install from https://python.org'
foreach ($candidate in @('py -3.11', 'py -3', 'python')) {
    $parts = $candidate -split '\s+'
    $exe = $parts[0]
    $prefix = ''
    if ($parts.Count -gt 1) {
        $prefix = ($parts[1..($parts.Count - 1)] -join ' ') + ' '
    }
    $probe = Invoke-Capture -FilePath $exe -Arguments ($prefix + '--version')
    if ($probe.Started -and $probe.ExitCode -eq 0 -and
        (Test-MinimumVersion -VersionString $probe.Output -Minimum '3.11')) {
        $pythonOk = $true
        $pythonDetail = "$candidate -> $($probe.Output)"
        break
    }
}
Write-Result -Ok $pythonOk -Label 'python launcher 3.11+' -Detail $pythonDetail

# ── 2. Virtualenv ──────────────────────────────────────────────────────

Write-Heading 'Virtualenv'

$venvPython = Join-Path $script:RepoRoot '.venv\Scripts\python.exe'
$venvOk = Test-Path $venvPython
$venvDetail = $venvPython
if (-not $venvOk) {
    $venvDetail = "not found; run .\windows\install.ps1 first"
}
Write-Result -Ok $venvOk -Label 'repo virtualenv (.venv\Scripts\python.exe)' -Detail $venvDetail

if (-not $venvOk) {
    Write-Host ""
    Write-Host "Cannot continue without the virtualenv. Run .\windows\install.ps1" -ForegroundColor Yellow
    exit 1
}

# ── 3. Package import ──────────────────────────────────────────────────

Write-Heading 'Packages'

$import = Invoke-Capture -FilePath $venvPython `
    -Arguments '-c "import broadcast, engine"' `
    -WorkingDirectory $script:RepoRoot
$importOk = ($import.ExitCode -eq 0)
$importDetail = 'broadcast + engine importable'
if (-not $importOk) {
    $importDetail = $import.Output
}
Write-Result -Ok $importOk -Label 'broadcast and engine installed' -Detail $importDetail

# ── 4-6. Binaries and Icecast share directories ────────────────────────

Write-Heading 'Engine paths'

$radioExe = Join-Path $script:RepoRoot '.venv\Scripts\radio.exe'
if (-not (Test-Path $radioExe)) {
    # Fall back to the module form when the console script is missing.
    $paths = Invoke-Capture -FilePath $venvPython `
        -Arguments '-m engine paths --show' `
        -WorkingDirectory (Join-Path $script:RepoRoot 'liquidsoap')
}
else {
    $paths = Invoke-Capture -FilePath $radioExe -Arguments 'paths --show' `
        -WorkingDirectory (Join-Path $script:RepoRoot 'liquidsoap')
}

if ($paths.Output) {
    Write-Host $paths.Output -ForegroundColor DarkGray
}

$platform = Get-PathsValue -Output $paths.Output -Key 'platform'
$icecastBin = Get-PathsValue -Output $paths.Output -Key 'icecast'
$liquidsoapBin = Get-PathsValue -Output $paths.Output -Key 'liquidsoap'
$webRoot = Get-PathsValue -Output $paths.Output -Key 'icecast-webroot'
$adminRoot = Get-PathsValue -Output $paths.Output -Key 'icecast-adminroot'

Write-Result -Ok ($platform -eq 'Windows') -Label 'engine reports Windows platform' -Detail "platform: $platform"

$liquidsoapOk = ($liquidsoapBin -ne '' -and $liquidsoapBin -like '*.exe')
$liquidsoapDetail = $liquidsoapBin
if ($liquidsoapBin -eq '') {
    $liquidsoapDetail = 'not resolvable; set LIQUIDSOAP_BIN or install liquidsoap'
}
elseif (-not $liquidsoapOk) {
    $liquidsoapDetail = "expected a .exe path, got: $liquidsoapBin"
}
Write-Result -Ok $liquidsoapOk -Label 'liquidsoap.exe resolvable' -Detail $liquidsoapDetail

$icecastOk = ($icecastBin -ne '' -and $icecastBin -like '*.exe')
$icecastDetail = $icecastBin
if ($icecastBin -eq '') {
    $icecastDetail = 'not resolvable; set ICECAST_BIN or install Icecast'
}
elseif (-not $icecastOk) {
    $icecastDetail = "expected a .exe path, got: $icecastBin"
}
Write-Result -Ok $icecastOk -Label 'icecast.exe resolvable' -Detail $icecastDetail

$webOk = ($webRoot -ne '' -and (Test-Path $webRoot))
$webDetail = $webRoot
if ($webRoot -eq '') {
    $webDetail = 'not resolved; set ICECAST_WEBROOT to the Icecast web directory'
}
elseif (-not $webOk) {
    $webDetail = "resolved but missing on disk: $webRoot"
}
Write-Result -Ok $webOk -Label 'icecast webroot directory' -Detail $webDetail

$adminOk = ($adminRoot -ne '' -and (Test-Path $adminRoot))
$adminDetail = $adminRoot
if ($adminRoot -eq '') {
    $adminDetail = 'not resolved; set ICECAST_ADMINROOT to the Icecast admin directory'
}
elseif (-not $adminOk) {
    $adminDetail = "resolved but missing on disk: $adminRoot"
}
Write-Result -Ok $adminOk -Label 'icecast adminroot directory' -Detail $adminDetail

# ── 7. Dry run ─────────────────────────────────────────────────────────

Write-Heading 'Start dry run'

if (Test-Path $radioExe) {
    $dry = Invoke-Capture -FilePath $radioExe -Arguments 'start --dry-run' `
        -WorkingDirectory (Join-Path $script:RepoRoot 'liquidsoap')
}
else {
    $dry = Invoke-Capture -FilePath $venvPython `
        -Arguments '-m engine start --dry-run' `
        -WorkingDirectory (Join-Path $script:RepoRoot 'liquidsoap')
}

if ($dry.Output) {
    Write-Host $dry.Output -ForegroundColor DarkGray
}

$dryOk = ($dry.ExitCode -eq 0 -and $dry.Output -match 'DRY RUN: would start icecast at')
$dryDetail = 'secrets, rendered config and binaries all validated'
if (-not $dryOk) {
    $dryDetail = "radio start --dry-run exited $($dry.ExitCode)"
}
Write-Result -Ok $dryOk -Label 'radio start --dry-run' -Detail $dryDetail

# ── summary ────────────────────────────────────────────────────────────

Write-Heading 'Summary'

if ($script:Failures -eq 0) {
    Write-Host "All checks passed. Start the station with:" -ForegroundColor Green
    Write-Host "  .venv\Scripts\radio start" -ForegroundColor White
    Write-Host "  .venv\Scripts\radio status" -ForegroundColor White
    exit 0
}

Write-Host "$script:Failures check(s) failed." -ForegroundColor Red
Write-Host "See windows\README.md and liquidsoap\engine\README.md for fixes." -ForegroundColor Yellow
exit 1
