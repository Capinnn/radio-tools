#Requires -Version 5.1
<#
.SYNOPSIS
    Install the radio stack on Windows.

.DESCRIPTION
    Idempotent PowerShell installer that prepares the repo virtualenv,
    the studio virtualenv, and the Liquidsoap secrets file. Does not modify
    broadcast/, studio/ or liquidsoap/ source code.

.EXAMPLE
    .\windows\install.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Heading {
    param([string]$Text)
    Write-Host "`n=== $Text ===" -ForegroundColor Cyan
}

function Write-Subheading {
    param([string]$Text)
    Write-Host "`n-- $Text" -ForegroundColor DarkCyan
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

function Get-PythonCommand {
    $candidates = @('py -3.11', 'py -3', 'python')
    foreach ($candidate in $candidates) {
        try {
            $parts = $candidate -split '\s+'
            $cmdName = $parts[0]
            $cmdArgs = ($parts[1..100] -join ' ') + ' --version'
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $cmdName
            $psi.Arguments = $cmdArgs
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
            # Try the next candidate.
        }
    }
    return $null
}

function Find-Binary {
    param(
        [string]$Name,
        [string[]]$ExtraPaths
    )
    $pathCmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($pathCmd) {
        return $pathCmd.Source
    }
    foreach ($dir in $ExtraPaths) {
        $candidate = Join-Path $dir $Name
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

function New-RandomPassword {
    param([int]$Length = 16)
    # URL-safe characters that keep Icecast XML / Liquidsoap environment
    # substitution happy: letters, digits, and - _ . ~ ! @ % + = : , /
    $chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~!%@+=:,/'
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $bytes = New-Object byte[] $Length
    $rng.GetBytes($bytes)
    $password = ''
    for ($i = 0; $i -lt $Length; $i++) {
        $password += $chars[$bytes[$i] % $chars.Length]
    }
    return $password
}

function Invoke-Python {
    param(
        [string]$PythonCommand,
        [string]$Arguments,
        [string]$WorkingDirectory = $RepoRoot
    )
    $parts = $PythonCommand -split '\s+'
    $cmdName = $parts[0]
    $cmdArgs = ($parts[1..100] -join ' ') + " $Arguments"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cmdName
    $psi.Arguments = $cmdArgs
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.WaitForExit()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    if ($proc.ExitCode -ne 0) {
        throw "Command failed (exit $($proc.ExitCode)): $PythonCommand $Arguments`nSTDOUT:`n$stdout`nSTDERR:`n$stderr"
    }
    return $stdout, $stderr
}

# ---------------------------------------------------------------------------
# 1. Python 3.11+
# ---------------------------------------------------------------------------
Write-Heading "Checking Python"
$pythonInfo = Get-PythonCommand
if (-not $pythonInfo) {
    Write-Host "ERROR: Python 3.11 or newer is required." -ForegroundColor Red
    Write-Host @"

Install Python 3.11+ from https://www.python.org/downloads/
During setup, check "Add python.exe to PATH" (or "Add Python to environment variables").
Then open a new PowerShell window and re-run this installer.
"@
    exit 1
}

Write-Host "OK: $($pythonInfo.Version) (using '$($pythonInfo.Command)')" -ForegroundColor Green
$PythonCommand = $pythonInfo.Command

# ---------------------------------------------------------------------------
# 2. Engine binaries (Liquidsoap + Icecast)
# ---------------------------------------------------------------------------
Write-Heading "Checking streaming engine binaries"

$liquidsoapPaths = @(
    "${env:ProgramFiles}\Liquidsoap"
    "${env:ProgramFiles(x86)}\Liquidsoap"
)
$icecastPaths = @(
    "${env:ProgramFiles}\Icecast"
    "${env:ProgramFiles(x86)}\Icecast"
)

$liquidsoapExe = Find-Binary -Name 'liquidsoap.exe' -ExtraPaths $liquidsoapPaths
$icecastExe = Find-Binary -Name 'icecast.exe' -ExtraPaths $icecastPaths
if (-not $icecastExe) {
    $icecastExe = Find-Binary -Name 'icecast2.exe' -ExtraPaths $icecastPaths
}

if ($liquidsoapExe) {
    Write-Host "OK: liquidsoap.exe found at $liquidsoapExe" -ForegroundColor Green
}
else {
    Write-Host "WARNING: liquidsoap.exe not found." -ForegroundColor Yellow
    Write-Host "  Download and install it from https://www.liquidsoap.info/doc-dev/install.html" -ForegroundColor Yellow
    Write-Host "  The engine scripts will not run until liquidsoap.exe is on PATH or installed under ${env:ProgramFiles}\Liquidsoap." -ForegroundColor Yellow
}

if ($icecastExe) {
    Write-Host "OK: icecast binary found at $icecastExe" -ForegroundColor Green
}
else {
    Write-Host "WARNING: icecast.exe / icecast2.exe not found." -ForegroundColor Yellow
    Write-Host "  Download and install it from https://icecast.org/download/" -ForegroundColor Yellow
    Write-Host "  The stream server will not run until icecast is on PATH or installed under ${env:ProgramFiles}\Icecast." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 3. Repo virtualenv + broadcast package
# ---------------------------------------------------------------------------
Write-Heading "Preparing broadcast toolkit"
$rootVenv = Join-Path $RepoRoot '.venv'
$rootPip = Join-Path $rootVenv 'Scripts\pip.exe'

if (-not (Test-Path $rootPip)) {
    Write-Subheading "Creating repo virtualenv at $rootVenv"
    $null = Invoke-Python -PythonCommand $PythonCommand -Arguments "-m venv `"$rootVenv`"" -WorkingDirectory $RepoRoot
}
else {
    Write-Host "Repo virtualenv already exists at $rootVenv" -ForegroundColor Green
}

Write-Subheading "Installing broadcast package in editable mode"
$null = Invoke-Python -PythonCommand (Join-Path $rootVenv 'Scripts\python.exe') -Arguments "-m pip install --upgrade pip" -WorkingDirectory $RepoRoot
$null = Invoke-Python -PythonCommand (Join-Path $rootVenv 'Scripts\python.exe') -Arguments "-m pip install -e `"$RepoRoot`"" -WorkingDirectory $RepoRoot

# Optional dev dependencies (pytest) are not required for runtime; include them
# if the user wants to run tests.
try {
    $null = Invoke-Python -PythonCommand (Join-Path $rootVenv 'Scripts\python.exe') -Arguments "-m pip install -e `"$RepoRoot`"[dev]" -WorkingDirectory $RepoRoot
    Write-Host "Installed broadcast with dev dependencies." -ForegroundColor Green
}
catch {
    Write-Host "Note: broadcast dev dependencies could not be installed; runtime commands are still available." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 4. Studio virtualenv + Flask deps
# ---------------------------------------------------------------------------
Write-Heading "Preparing studio console"
$studioDir = Join-Path $RepoRoot 'studio'
$studioVenv = Join-Path $studioDir '.venv'
$studioPip = Join-Path $studioVenv 'Scripts\pip.exe'
$studioReqs = Join-Path $studioDir 'requirements.txt'

if (-not (Test-Path $studioPip)) {
    Write-Subheading "Creating studio virtualenv at $studioVenv"
    $null = Invoke-Python -PythonCommand $PythonCommand -Arguments "-m venv `"$studioVenv`"" -WorkingDirectory $studioDir
}
else {
    Write-Host "Studio virtualenv already exists at $studioVenv" -ForegroundColor Green
}

Write-Subheading "Installing studio requirements"
$null = Invoke-Python -PythonCommand (Join-Path $studioVenv 'Scripts\python.exe') -Arguments "-m pip install --upgrade pip" -WorkingDirectory $studioDir
$null = Invoke-Python -PythonCommand (Join-Path $studioVenv 'Scripts\python.exe') -Arguments "-m pip install -r `"$studioReqs`"" -WorkingDirectory $studioDir

# psutil is not currently required but is commonly useful for runtime checks.
try {
    $null = Invoke-Python -PythonCommand (Join-Path $studioVenv 'Scripts\python.exe') -Arguments "-m pip install psutil" -WorkingDirectory $studioDir
    Write-Host "Installed optional psutil." -ForegroundColor Green
}
catch {
    Write-Host "Note: optional psutil was not installed." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 5. Liquidsoap secrets.env
# ---------------------------------------------------------------------------
Write-Heading "Preparing Liquidsoap/Icecast secrets"
$secretsDir = Join-Path $RepoRoot 'liquidsoap\config'
$secretsExample = Join-Path $secretsDir 'secrets.env.example'
$secretsFile = Join-Path $secretsDir 'secrets.env'

if (-not (Test-Path $secretsFile)) {
    Write-Subheading "Generating $secretsFile"
    if (-not (Test-Path $secretsExample)) {
        throw "Could not find $secretsExample; cannot generate secrets.env."
    }
    $template = Get-Content $secretsExample -Raw
    $password = New-RandomPassword -Length 16
    $generated = $template -replace 'ICECAST_SOURCE_PASSWORD=.*', "ICECAST_SOURCE_PASSWORD=$password"
    Set-Content -Path $secretsFile -Value $generated -NoNewline -Encoding UTF8

    # ACL note: on Windows, restricting a file to the current user is the
    # equivalent of chmod 600. Keep the note in the generated file itself.
    $note = "`n# Windows ACL: right-click the file -> Properties -> Security -> Advanced ->`n# Disable inheritance, remove groups other than your own user, set your user to Full control.`n"
    Add-Content -Path $secretsFile -Value $note -Encoding UTF8

    # Try to lock the file down automatically.
    try {
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = Get-Acl $secretsFile
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $currentUser,
            'Read,Write',
            'None',
            'None',
            'Allow'
        )
        $acl.SetAccessRule($rule)
        Set-Acl $secretsFile $acl | Out-Null
        Write-Host "Restricted $secretsFile to $currentUser." -ForegroundColor Green
    }
    catch {
        Write-Host "Note: could not tighten ACLs on $secretsFile. Review the ACL note inside the file." -ForegroundColor Yellow
    }
}
else {
    Write-Host "Secrets file already exists at $secretsFile (left untouched)." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 6. Quick start
# ---------------------------------------------------------------------------
Write-Heading "Installation complete"
Write-Host @"

Quick start (run each in its own PowerShell window from $RepoRoot):

1. Start the studio console:
   cd $RepoRoot\studio
   .venv\Scripts\python app.py --scan
   Open http://127.0.0.1:5110 in your browser.

2. Generate the first playlist:
   cd $RepoRoot
   .venv\Scripts\python -m broadcast.playlistgen studio\music --rotation liquidsoap\config\rotation.json --hour (Get-Date -Format HH) --slot 1h --output liquidsoap\data\playlist.m3u

3. Start the engine. On Windows the station script is used.
   The engine manager is in liquidsoap\engine\. See liquidsoap\engine\README.md
   for the exact `radio start` command.

4. Listen to the stream at:
   http://127.0.0.1:8000/radio.mp3

If a firewall prompt appears for Icecast, allow it on private networks.
Port 8000 must be reachable if you want other devices on the same LAN to listen.
"@ -ForegroundColor Cyan

# Run the prereq checker as a final sanity check, but do not fail the install
# if only optional warnings remain (install may have placed the binaries
# during the run and PATH has not been refreshed).
Write-Heading "Running prerequisite check"
$checkScript = Join-Path $PSScriptRoot 'check-prereqs.ps1'
if (Test-Path $checkScript) {
    & $checkScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nSome optional prerequisites are still missing (see above)." -ForegroundColor Yellow
    }
}
else {
    Write-Host "check-prereqs.ps1 not found next to installer; skipping final check." -ForegroundColor Yellow
}

exit 0
