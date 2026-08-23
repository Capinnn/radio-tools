#Requires -Version 5.1
<#
.SYNOPSIS
    Verify that the radio stack has everything it needs on Windows.

.DESCRIPTION
    Checks Python 3.11+, liquidsoap.exe and icecast.exe / icecast2.exe.
    Prints a readable list of what is found and what is missing, then exits
    with code 0 when everything is present or non-zero when something is missing.

.EXAMPLE
    .\windows\check-prereqs.ps1
    $LASTEXITCODE
#>

[CmdletBinding()]
param()

$script:Missing = @()
$script:Found = @()

function Write-Heading {
    param([string]$Text)
    Write-Host "`n=== $Text ===" -ForegroundColor Cyan
}

function Test-MinimumVersion {
    param(
        [string]$VersionString,
        [version]$Minimum
    )
    # Strip a leading "Python " if present and keep the first dotted token.
    $clean = ($VersionString -replace '^[^0-9]*', '') -split '\s+' | Select-Object -First 1
    [version]$parsed = $null
    if ([version]::TryParse($clean, [ref]$parsed)) {
        return ($parsed -ge $Minimum)
    }
    return $false
}

function Get-PythonCommand {
    # Prefer the Windows Python launcher pinned to 3.11+, then any python on PATH.
    $candidates = @('py -3.11', 'py -3', 'python')
    foreach ($candidate in $candidates) {
        try {
            $cmdName = ($candidate -split '\s+')[0]
            $cmdArgs = ($candidate -split '\s+')[1..100] -join ' '
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $cmdName
            $psi.Arguments = if ($cmdArgs) { "$cmdArgs --version" } else { "--version" }
            $psi.UseShellExecute = $false
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.CreateNoWindow = $true
            $proc = [System.Diagnostics.Process]::Start($psi)
            $proc.WaitForExit()
            $output = ($proc.StandardOutput.ReadToEnd() + $proc.StandardError.ReadToEnd()).Trim()
            if ($proc.ExitCode -eq 0 -and (Test-MinimumVersion -VersionString $output -Minimum '3.11')) {
                return @{ Command = $candidate; Version = $output }
            }
        }
        catch {
            # Candidate not available; try the next one.
        }
    }
    return $null
}

function Find-Binary {
    param(
        [string]$Name,
        [string[]]$ExtraPaths
    )
    # PATH lookup.
    $pathCmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($pathCmd) {
        return $pathCmd.Source
    }
    # Standard install directories.
    foreach ($dir in $ExtraPaths) {
        $candidate = Join-Path $dir $Name
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
Write-Heading "Python"
$pythonInfo = Get-PythonCommand
if ($pythonInfo) {
    $script:Found += "Python $($pythonInfo.Version) via '$($pythonInfo.Command)'"
    Write-Host "OK: $($pythonInfo.Version) (command: $($pythonInfo.Command))" -ForegroundColor Green
}
else {
    $script:Missing += "Python 3.11 or newer (install from https://www.python.org/downloads/ and ensure 'py' or 'python' is on PATH)"
    Write-Host "MISSING: Python 3.11+ not found on PATH" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Liquidsoap
# ---------------------------------------------------------------------------
Write-Heading "Liquidsoap"
$liquidsoapPaths = @(
    "${env:ProgramFiles}\Liquidsoap"
    "${env:ProgramFiles(x86)}\Liquidsoap"
)
$liquidsoapExe = Find-Binary -Name 'liquidsoap.exe' -ExtraPaths $liquidsoapPaths
if ($liquidsoapExe) {
    $script:Found += "liquidsoap.exe at $liquidsoapExe"
    Write-Host "OK: $liquidsoapExe" -ForegroundColor Green
}
else {
    $script:Missing += "liquidsoap.exe (download from https://www.liquidsoap.info/doc-dev/install.html)"
    Write-Host "MISSING: liquidsoap.exe not on PATH or in standard install directories" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Icecast
# ---------------------------------------------------------------------------
Write-Heading "Icecast"
$icecastPaths = @(
    "${env:ProgramFiles}\Icecast"
    "${env:ProgramFiles(x86)}\Icecast"
)
$icecastExe = Find-Binary -Name 'icecast.exe' -ExtraPaths $icecastPaths
if (-not $icecastExe) {
    $icecastExe = Find-Binary -Name 'icecast2.exe' -ExtraPaths $icecastPaths
}
if ($icecastExe) {
    $script:Found += "icecast binary at $icecastExe"
    Write-Host "OK: $icecastExe" -ForegroundColor Green
}
else {
    $script:Missing += "icecast.exe or icecast2.exe (download from https://icecast.org/download/)"
    Write-Host "MISSING: icecast.exe / icecast2.exe not on PATH or in standard install directories" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Heading "Summary"
if ($script:Found.Count -gt 0) {
    Write-Host "Found:" -ForegroundColor Green
    $script:Found | ForEach-Object { Write-Host "  - $_" }
}
if ($script:Missing.Count -gt 0) {
    Write-Host "Missing:" -ForegroundColor Red
    $script:Missing | ForEach-Object { Write-Host "  - $_" }
    Write-Host "`nInstall missing items, then run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host "`nAll Windows prerequisites are satisfied." -ForegroundColor Green
exit 0
