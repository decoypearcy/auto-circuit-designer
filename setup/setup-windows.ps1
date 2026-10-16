<#
.SYNOPSIS
  Auto Circuit Designer: Windows 10 setup (PowerShell 5.1 compatible, idempotent).

.DESCRIPTION
  Checks each component, installs what is missing at the version pinned in versions.yaml
  (winget first, then a documented direct download), creates the project and library folders,
  sets user environment variables, and merges the "kicad" and "acd" MCP servers into
  %APPDATA%\Claude\claude_desktop_config.json (after backing the file up).
  Safe to run again: finished steps are detected and skipped.

.PARAMETER ProjectsRoot
  Folder for PCB projects (ACD_PROJECTS_ROOT). Prompted for when omitted.

.PARAMETER LibraryRoot
  Folder for the shared component library (ACD_LIBRARY_ROOT). Prompted for when omitted.

.PARAMETER NoPrompt
  Never ask questions; use the parameter, the existing env var, or the default.

.PARAMETER KiCadVersion
  Override the pinned KiCad version (for example 10.0.6 if 10.0.7 cannot be downloaded yet).

.PARAMETER SkipConfigMerge
  Do everything except editing claude_desktop_config.json.

.PARAMETER InstallRoot
  Install KiCad, Node.js, Java and git under this folder (for example "D:\Program Files")
  instead of the Windows defaults. winget is skipped for those four and the official
  installers are used with an install-folder argument (same checksum and signature checks).
  uv, Freerouting and the base MCP stay in the user profile (they are per-user tools).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup\setup-windows.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectsRoot,
    [string]$LibraryRoot,
    [switch]$NoPrompt,
    [string]$KiCadVersion,
    [switch]$SkipConfigMerge,
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster in PS 5.1
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$SetupDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot     = Split-Path -Parent $SetupDir
$CompanionDir = Join-Path $RepoRoot 'companion'
$ProfilePath  = Join-Path $RepoRoot 'profiles\pcbway-standard-2layer.yaml'
$VersionsFile = Join-Path $SetupDir 'versions.yaml'
$AcdHome      = Join-Path $env:LOCALAPPDATA 'acd'
$FrDir        = Join-Path $AcdHome 'freerouting'
$McpDir       = Join-Path $AcdHome 'kicad-mcp-server'
$ClaudeDir    = Join-Path $env:APPDATA 'Claude'
$ClaudeConfig = Join-Path $ClaudeDir 'claude_desktop_config.json'

$script:Results = New-Object System.Collections.ArrayList
$script:Failed  = $false

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host "   $Text" }
function Write-Good([string]$Text) { Write-Host "   [ok]   $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "   [warn] $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host "   [FAIL] $Text" -ForegroundColor Red }

function Add-Result([string]$Item, [string]$Status, [string]$Detail) {
    [void]$script:Results.Add([pscustomobject]@{ Item = $Item; Status = $Status; Detail = $Detail })
    if ($Status -eq 'FAIL') { $script:Failed = $true }
}

# ---------------------------------------------------------------------------
# Pins (versions.yaml)
# ---------------------------------------------------------------------------
function Read-Pins([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "versions.yaml not found at $Path" }
    $pins = @{}
    $section = ''
    foreach ($raw in (Get-Content -LiteralPath $Path)) {
        $line = $raw.TrimEnd()
        if ($line.Trim() -eq '' -or $line -match '^\s*#') { continue }
        if ($line -match '^([A-Za-z0-9_]+):\s*(.*)$') {
            $k = $Matches[1]; $v = $Matches[2]
            if ($v -eq '') { $section = $k } else { $pins[$k] = (ConvertTo-PinValue $v); $section = '' }
        }
        elseif ($line -match '^\s+([A-Za-z0-9_]+):\s*(.*)$') {
            $pins[($section + '.' + $Matches[1])] = (ConvertTo-PinValue $Matches[2])
        }
    }
    return $pins
}

function ConvertTo-PinValue([string]$v) {
    $v = $v.Trim()
    if ($v -match '^"(.*)"\s*(#.*)?$') { return $Matches[1] }
    return ($v -replace '\s+#.*$', '')
}

$Pins = Read-Pins $VersionsFile
if ($KiCadVersion) { $Pins['kicad.version'] = $KiCadVersion }

function Expand-Url([string]$Template, [string]$Version) { return $Template.Replace('{version}', $Version) }

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
function Update-SessionPath {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (($m, $u) | Where-Object { $_ }) -join ';'
}

function Get-ToolPath([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function ConvertTo-ArgString([string[]]$ArgList) {
    $parts = foreach ($a in $ArgList) {
        if ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' } else { $a }
    }
    return ($parts -join ' ')
}

# Runs a program, captures stdout+stderr, enforces a timeout. Avoids PS 5.1 native stderr problems.
function Invoke-Capture {
    param(
        [string]$File,
        [string[]]$ArgList = @(),
        [int]$TimeoutSec = 60,
        [string]$WorkDir,
        [hashtable]$Env
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = ConvertTo-ArgString $ArgList
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkDir) { $psi.WorkingDirectory = $WorkDir }
    if ($Env) { foreach ($k in $Env.Keys) { $psi.EnvironmentVariables[$k] = [string]$Env[$k] } }
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    $so = $p.StandardOutput.ReadToEndAsync()
    $se = $p.StandardError.ReadToEndAsync()
    $done = $p.WaitForExit($TimeoutSec * 1000)
    if (-not $done) {
        try { $p.Kill() } catch { }
        return [pscustomobject]@{ ExitCode = -1; TimedOut = $true; Output = 'timed out after ' + $TimeoutSec + ' s' }
    }
    $p.WaitForExit()
    $text = (($so.Result + "`n" + $se.Result).Trim())
    return [pscustomobject]@{ ExitCode = $p.ExitCode; TimedOut = $false; Output = $text }
}

function Get-FirstLine([string]$Text) {
    if (-not $Text) { return '' }
    return (($Text -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -First 1)
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

function Save-Download([string]$Url, [string]$Dest) {
    $dir = Split-Path -Parent $Dest
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Write-Info "downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

function Test-Winget { return [bool](Get-ToolPath 'winget') }

# With -InstallRoot the program installs bypass winget so the folder can be set explicitly.
function Get-InstallDir([string]$Sub) {
    if (-not $InstallRoot) { return $null }
    return (Join-Path $InstallRoot $Sub)
}

function Assert-Signed([string]$Path, [string]$Signer, [string]$What) {
    $sig = Get-AuthenticodeSignature -FilePath $Path
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike ('*' + $Signer + '*')) {
        Remove-Item -LiteralPath $Path -Force
        throw "$What installer signature check failed. File deleted."
    }
    Write-Good "$What installer signature valid ($Signer)"
}

# Returns $true if winget installed the package. Never throws.
function Install-WithWinget([string]$Id, [string]$Version) {
    if (-not (Test-Winget)) { Write-Warn2 'winget is not available on this PC.'; return $false }
    $wargs = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($Version) { $wargs += @('--version', $Version) }
    Write-Info ("winget " + ($wargs -join ' '))
    try {
        & winget @wargs
        $code = $LASTEXITCODE
    } catch {
        Write-Warn2 "winget failed to start: $($_.Exception.Message)"
        return $false
    }
    # 0 = installed. -1978335189 (0x8A15002B) = nothing to do / already installed.
    if ($code -eq 0 -or $code -eq -1978335189) { Update-SessionPath; return $true }
    Write-Warn2 "winget exit code $code for $Id $Version"
    return $false
}

# Starts an installer elevated (UAC prompt) and waits. Returns exit code.
function Start-Elevated([string]$File, [string]$ArgString) {
    $p = Start-Process -FilePath $File -ArgumentList $ArgString -Verb RunAs -Wait -PassThru
    return $p.ExitCode
}

function Set-UserEnv([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    Set-Item -Path ("Env:" + $Name) -Value $Value
}

function Get-MajorVersion([string]$Text) {
    # Parses the first number in strings like: v24.22.0, openjdk version "25.0.4.1", 1.8.0_402
    if ($Text -match '"?v?(\d+)\.(\d+)') {
        if ($Matches[1] -eq '1') { return [int]$Matches[2] }
        return [int]$Matches[1]
    }
    if ($Text -match '"?v?(\d+)') { return [int]$Matches[1] }
    return 0
}

# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------
function Find-KiCad {
    $roots = @()
    if ($InstallRoot) { $roots += (Join-Path $InstallRoot 'KiCad\10.0') }
    $roots += @(
        (Join-Path $env:ProgramFiles 'KiCad\10.0'),
        (Join-Path ${env:ProgramFiles(x86)} 'KiCad\10.0'),
        (Join-Path $env:LOCALAPPDATA 'Programs\KiCad\10.0')
    )
    foreach ($r in $roots) {
        $cli = Join-Path $r 'bin\kicad-cli.exe'
        if (Test-Path -LiteralPath $cli) {
            return [pscustomobject]@{
                Root         = $r
                Cli          = $cli
                Python       = Join-Path $r 'bin\python.exe'
                DistPackages = Join-Path $r 'lib\python3\dist-packages'
            }
        }
    }
    return $null
}

function Get-JavaInfo([string]$JavaExe) {
    $r = Invoke-Capture -File $JavaExe -ArgList @('-version') -TimeoutSec 30
    $line = ($r.Output -split "`r?`n" | Where-Object { $_ -match 'version' } | Select-Object -First 1)
    return [pscustomobject]@{ Exe = $JavaExe; Line = $line; Major = (Get-MajorVersion $line) }
}

function Find-Java([int]$MinMajor) {
    # Returns @{ OnPath = info of first java on PATH or $null; Good = first java >= MinMajor or $null }
    $onPath = $null
    $p = Get-ToolPath 'java'
    if ($p) { $onPath = Get-JavaInfo $p }
    $good = $null
    if ($onPath -and $onPath.Major -ge $MinMajor) { $good = $onPath }
    if (-not $good) {
        $cands = New-Object System.Collections.ArrayList
        if ($env:JAVA_HOME) { [void]$cands.Add((Join-Path $env:JAVA_HOME 'bin\java.exe')) }
        $globs = @()
        if ($InstallRoot) { $globs += (Join-Path $InstallRoot 'Eclipse Adoptium\*\bin\java.exe') }
        $globs += @(
            (Join-Path $env:ProgramFiles 'Eclipse Adoptium\*\bin\java.exe'),
            (Join-Path $env:ProgramFiles 'Java\*\bin\java.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Eclipse Adoptium\*\bin\java.exe')
        )
        foreach ($g in $globs) {
            Get-ChildItem -Path $g -ErrorAction SilentlyContinue | ForEach-Object { [void]$cands.Add($_.FullName) }
        }
        foreach ($c in $cands) {
            if (Test-Path -LiteralPath $c) {
                $i = Get-JavaInfo $c
                if ($i.Major -ge $MinMajor) { $good = $i; break }
            }
        }
    }
    return @{ OnPath = $onPath; Good = $good }
}

# ---------------------------------------------------------------------------
# Component installers
# ---------------------------------------------------------------------------
function Confirm-Git {
    Write-Step 'git'
    $ver = $Pins['git.version']
    if (Get-ToolPath 'git') {
        $r = Invoke-Capture -File (Get-ToolPath 'git') -ArgList @('--version')
        Write-Good (Get-FirstLine $r.Output)
        Add-Result 'git' 'OK' (Get-FirstLine $r.Output); return
    }
    $dir = Get-InstallDir 'Git'
    $ok = $false
    if (-not $dir) { $ok = Install-WithWinget $Pins['git.winget_id'] $ver }
    if (-not $ok) {
        $dest = Join-Path $env:TEMP ("Git-$ver-64-bit.exe")
        Save-Download $Pins['git.exe_url'] $dest
        Assert-Signed $dest $Pins['git.signer'] 'Git'
        $gargs = '/VERYSILENT /NORESTART'
        if ($dir) { $gargs += " /DIR=`"$dir`"" }
        $code = Start-Elevated $dest $gargs
        if ($code -ne 0) { throw "Git installer exit code $code" }
        Update-SessionPath
    }
    if (-not (Get-ToolPath 'git')) { throw 'git not found on PATH after install. Open a new PowerShell window and rerun.' }
    Write-Good ("installed git " + $ver)
    Add-Result 'git' 'INSTALLED' $ver
}

function Confirm-Node {
    Write-Step 'Node.js'
    $ver = $Pins['node.version']
    $min = [int]$Pins['node.min_major']
    $node = Get-ToolPath 'node'
    if ($node) {
        $r = Invoke-Capture -File $node -ArgList @('-v')
        $major = Get-MajorVersion $r.Output
        if ($major -ge $min) {
            Write-Good ("node " + (Get-FirstLine $r.Output) + " (needs $min or higher)")
            Add-Result 'node' 'OK' (Get-FirstLine $r.Output); return
        }
        Write-Warn2 ("node " + (Get-FirstLine $r.Output) + " is older than $min, installing $ver")
    }
    $dir = Get-InstallDir 'nodejs'
    $ok = $false
    if (-not $dir) { $ok = Install-WithWinget $Pins['node.winget_id'] $ver }
    if (-not $ok) {
        Write-Info 'using the official MSI'
        $msiUrl = Expand-Url $Pins['node.msi_url'] $ver
        $dest = Join-Path $env:TEMP ("node-v$ver-x64.msi")
        Save-Download $msiUrl $dest
        $sums = (Invoke-WebRequest -Uri (Expand-Url $Pins['node.shasums_url'] $ver) -UseBasicParsing).Content
        $fileName = "node-v$ver-x64.msi"
        $expected = $null
        foreach ($l in ($sums -split "`r?`n")) {
            if ($l -match ('^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($fileName) + '\s*$')) { $expected = $Matches[1].ToLower() }
        }
        if (-not $expected) { throw "No SHA-256 for $fileName in SHASUMS256.txt" }
        if ((Get-FileSha256 $dest) -ne $expected) { Remove-Item -LiteralPath $dest -Force; throw 'Node MSI SHA-256 mismatch. File deleted.' }
        $margs = "/i `"$dest`" /qn /norestart"
        if ($dir) { $margs += " INSTALLDIR=`"$dir\`"" }
        $code = Start-Elevated 'msiexec.exe' $margs
        if ($code -ne 0 -and $code -ne 3010) { throw "Node MSI exit code $code" }
        Update-SessionPath
    }
    if (-not (Get-ToolPath 'node')) { throw 'node not found on PATH after install. Open a new PowerShell window and rerun.' }
    Write-Good ("installed node " + $ver)
    Add-Result 'node' 'INSTALLED' $ver
}

function Confirm-Java {
    Write-Step 'Java (Eclipse Temurin JRE)'
    $min = [int]$Pins['java.min_major']
    $found = Find-Java $min
    if (-not $found.Good) {
        $dir = Get-InstallDir ('Eclipse Adoptium\jre-' + $Pins['java.version'] + '-hotspot')
        $ok = $false
        if (-not $dir) { $ok = Install-WithWinget $Pins['java.winget_id'] $Pins['java.winget_version'] }
        if (-not $ok) {
            Write-Info 'using the Temurin MSI'
            $dest = Join-Path $env:TEMP 'temurin-jre-25.msi'
            Save-Download $Pins['java.msi_url'] $dest
            if ((Get-FileSha256 $dest) -ne $Pins['java.msi_sha256'].ToLower()) { Remove-Item -LiteralPath $dest -Force; throw 'Temurin MSI SHA-256 mismatch. File deleted.' }
            # FeatureMain + FeatureEnvironment (PATH). JAVA_HOME is deliberately not set, so other Java users are unaffected.
            $margs = "/i `"$dest`" /qn /norestart ADDLOCAL=FeatureMain,FeatureEnvironment"
            if ($dir) { $margs += " INSTALLDIR=`"$dir\`"" }
            $code = Start-Elevated 'msiexec.exe' $margs
            if ($code -ne 0 -and $code -ne 3010) { throw "Temurin MSI exit code $code" }
        }
        Update-SessionPath
        $found = Find-Java $min
        if (-not $found.Good) { throw "Java $min or higher not found after install." }
        Add-Result 'java' 'INSTALLED' $found.Good.Line
    } else {
        Add-Result 'java' 'OK' $found.Good.Line
    }
    Write-Good $found.Good.Line
    $script:JavaGood = $found.Good
    $script:JavaPathOk = ($found.OnPath -and $found.OnPath.Major -ge $min)
    if (-not $script:JavaPathOk) {
        Write-Warn2 'The first java on PATH is older than needed (or missing). The base MCP uses the first java on PATH.'
        Write-Info  ("The script will put " + (Split-Path -Parent $found.Good.Exe) + " first on PATH inside the kicad MCP entry.")
    }
}

function Confirm-KiCad {
    Write-Step 'KiCad 10.0'
    $ver = $Pins['kicad.version']
    $k = Find-KiCad
    if ($k) {
        $r = Invoke-Capture -File $k.Cli -ArgList @('version')
        $installed = Get-FirstLine $r.Output
        if ($installed -eq $ver) { Write-Good "kicad-cli $installed (matches pin)"; Add-Result 'kicad' 'OK' $installed }
        else { Write-Warn2 "kicad-cli $installed found, pinned is $ver. Leaving it alone."; Add-Result 'kicad' 'OK (differs from pin)' "$installed, pin $ver" }
        $script:KiCad = $k
        return
    }
    $dir = Get-InstallDir 'KiCad\10.0'
    $ok = $false
    if (-not $dir) { $ok = Install-WithWinget $Pins['kicad.winget_id'] $ver }
    if (-not $ok) {
        Write-Info 'using the direct installer download'
        $dest = Join-Path $env:TEMP ("kicad-$ver-x86_64.exe")
        $got = $false
        # Reuse an earlier, still validly signed download (a rerun after a failed install).
        if ((Test-Path -LiteralPath $dest) -and ((Get-AuthenticodeSignature -FilePath $dest).Status -eq 'Valid')) {
            Write-Info "reusing $dest"; $got = $true
        }
        if (-not $got) { foreach ($key in @('kicad.url_github', 'kicad.url_cern', 'kicad.url_aarnet', 'kicad.url_mit')) {
            try { Save-Download (Expand-Url $Pins[$key] $ver) $dest; $got = $true; break }
            catch { Write-Warn2 "download failed from $key" }
        } }
        if (-not $got) {
            throw ("Could not download KiCad $ver. If it is not published yet, rerun with -KiCadVersion " + $Pins['kicad.fallback_version'])
        }
        Assert-Signed $dest $Pins['kicad.signer'] 'KiCad'
        # KiCad uses NsisMultiUser: silent mode needs /allusers (or /currentuser), else exit 666660.
        # NSIS: /D= must be the last argument and is not quoted, even with spaces.
        $kargs = '/S /allusers'
        if ($dir) { $kargs += " /D=$dir" }
        $code = Start-Elevated $dest $kargs
        if ($code -ne 0) { throw "KiCad installer exit code $code" }
    }
    Update-SessionPath
    $k = Find-KiCad
    if (-not $k) { throw 'kicad-cli.exe not found under KiCad\10.0 after install.' }
    $script:KiCad = $k
    $r = Invoke-Capture -File $k.Cli -ArgList @('version')
    Write-Good ("installed kicad-cli " + (Get-FirstLine $r.Output))
    Add-Result 'kicad' 'INSTALLED' (Get-FirstLine $r.Output)
}

function Confirm-Uv {
    Write-Step 'uv'
    $ver = $Pins['uv.version']
    if (Get-ToolPath 'uv') {
        $r = Invoke-Capture -File (Get-ToolPath 'uv') -ArgList @('--version')
        Write-Good (Get-FirstLine $r.Output); Add-Result 'uv' 'OK' (Get-FirstLine $r.Output); return
    }
    $ok = Install-WithWinget $Pins['uv.winget_id'] $ver
    if (-not $ok) {
        Write-Info 'falling back to the pinned official install script'
        $dest = Join-Path $env:TEMP "uv-install-$ver.ps1"
        Save-Download $Pins['uv.install_script_url'] $dest
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $dest
        if ($LASTEXITCODE -ne 0) { throw "uv install script exit code $LASTEXITCODE" }
        Update-SessionPath
        $localBin = Join-Path $env:USERPROFILE '.local\bin'
        if ((Test-Path (Join-Path $localBin 'uv.exe')) -and ($env:Path -notlike "*$localBin*")) { $env:Path = $localBin + ';' + $env:Path }
    }
    if (-not (Get-ToolPath 'uv')) { throw 'uv not found on PATH after install. Open a new PowerShell window and rerun.' }
    Write-Good "installed uv $ver"
    Add-Result 'uv' 'INSTALLED' $ver
}

function Confirm-Freerouting {
    Write-Step 'Freerouting'
    $jar = Join-Path $FrDir $Pins['freerouting.jar_name']
    $want = $Pins['freerouting.jar_sha256'].ToLower()
    if (-not (Test-Path -LiteralPath $FrDir)) { New-Item -ItemType Directory -Force -Path $FrDir | Out-Null }
    if ((Test-Path -LiteralPath $jar) -and ((Get-FileSha256 $jar) -eq $want)) {
        Write-Good "$jar (checksum ok)"
        Add-Result 'freerouting' 'OK' $Pins['freerouting.version']
    } else {
        $tmp = $jar + '.download'
        Save-Download $Pins['freerouting.jar_url'] $tmp
        if ((Get-FileSha256 $tmp) -ne $want) { Remove-Item -LiteralPath $tmp -Force; throw 'Freerouting jar SHA-256 mismatch. File deleted.' }
        Move-Item -LiteralPath $tmp -Destination $jar -Force
        Write-Good "downloaded $jar"
        Add-Result 'freerouting' 'INSTALLED' $Pins['freerouting.version']
    }
    $script:FrJar = $jar
    Set-UserEnv 'FREEROUTING_JAR' $jar
}

function Confirm-BaseMcp {
    Write-Step 'Base MCP (mixelpixx/KiCAD-MCP-Server)'
    $tag = $Pins['base_mcp.tag']
    $commit = $Pins['base_mcp.commit']
    $git = Get-ToolPath 'git'
    if (-not (Test-Path -LiteralPath $AcdHome)) { New-Item -ItemType Directory -Force -Path $AcdHome | Out-Null }

    if (-not (Test-Path -LiteralPath (Join-Path $McpDir '.git'))) {
        Write-Info "cloning $tag into $McpDir"
        & $git clone --quiet --branch $tag --depth 1 $Pins['base_mcp.repo_url'] $McpDir
        if ($LASTEXITCODE -ne 0) { throw "git clone failed (exit $LASTEXITCODE)" }
    } else {
        $cur = Invoke-Capture -File $git -ArgList @('-C', $McpDir, 'describe', '--tags', '--exact-match')
        if ((Get-FirstLine $cur.Output) -ne $tag) {
            $dirty = Invoke-Capture -File $git -ArgList @('-C', $McpDir, 'status', '--porcelain', '--untracked-files=no')
            if ($dirty.Output.Trim()) { throw "$McpDir has local changes to tracked files. Move them aside, then rerun." }
            Write-Info "switching clone to $tag"
            & $git -C $McpDir fetch --quiet --depth 1 origin tag $tag
            if ($LASTEXITCODE -ne 0) { throw "git fetch failed (exit $LASTEXITCODE)" }
            & $git -C $McpDir checkout --quiet $tag
            if ($LASTEXITCODE -ne 0) { throw "git checkout failed (exit $LASTEXITCODE)" }
        }
    }
    $head = Get-FirstLine (Invoke-Capture -File $git -ArgList @('-C', $McpDir, 'rev-parse', 'HEAD')).Output
    if ($head -ne $commit) { throw "Tag $tag points at $head, expected $commit. Not building. Check versions.yaml." }
    Write-Good "$tag at $commit"

    $marker = Join-Path $McpDir '.acd-built'
    $dist = Join-Path $McpDir 'dist\index.js'
    $built = (Test-Path -LiteralPath $dist) -and (Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq "$tag $commit")
    if ($built) {
        Write-Good 'already built, skipping its setup-windows.ps1'
        Add-Result 'base MCP' 'OK' $tag
    } else {
        # The base project's own setup-windows.ps1 is not used: its KiCad detection only looks under C:\
        # and it hides npm and pip output. Its steps are done here instead: npm ci, npm run build, and
        # (below) pip install -r requirements.txt into KiCad's own Python.
        $npm = Get-ToolPath 'npm.cmd'
        if (-not $npm) { $npm = Join-Path (Split-Path -Parent (Get-ToolPath 'node')) 'npm.cmd' }
        if (-not (Test-Path -LiteralPath $npm)) { throw 'npm.cmd not found next to node.exe.' }
        # Package install scripts (esbuild) run "node" through cmd.exe; make sure node's folder is first on PATH.
        $nodeDir = Split-Path -Parent (Get-ToolPath 'node')
        $env:Path = $nodeDir + ';' + $env:Path
        Write-Info ("PATH has " + $env:Path.Length + " characters; node folder: $nodeDir")
        Push-Location $McpDir
        try {
            Write-Info 'npm ci (from package-lock.json)'
            & $npm ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw "npm ci exited with $LASTEXITCODE. Scroll up for its message." }
            Write-Info 'npm run build'
            & $npm run build
            if ($LASTEXITCODE -ne 0) { throw "npm run build exited with $LASTEXITCODE. Scroll up for its message." }
        } finally { Pop-Location }
        if (-not (Test-Path -LiteralPath $dist)) { throw 'dist\index.js missing after npm run build.' }
        Set-Content -LiteralPath $marker -Value "$tag $commit" -Encoding ASCII
        Write-Good 'built'
        Add-Result 'base MCP' 'INSTALLED' $tag
    }
    $script:McpIndex = $dist

    # Make sure the Python packages are importable by KiCad's own Python (the base script hides pip errors).
    $py = $script:KiCad.Python
    $probe = "import pcbnew, sexpdata, skip, PIL, pydantic, requests, colorlog, dotenv; print('imports ok', pcbnew.GetBuildVersion())"
    $envp = @{ PYTHONPATH = $script:KiCad.DistPackages }
    $r = Invoke-Capture -File $py -ArgList @('-c', $probe) -TimeoutSec 90 -Env $envp
    if ($r.ExitCode -ne 0) {
        Write-Warn2 'Python imports failed, trying pip install --user'
        $req = Join-Path $McpDir 'requirements.txt'
        $pip = Invoke-Capture -File $py -ArgList @('-m', 'pip', 'install', '--user', '-r', $req) -TimeoutSec 600 -Env $envp
        Write-Info (Get-FirstLine $pip.Output)
        $r = Invoke-Capture -File $py -ArgList @('-c', $probe) -TimeoutSec 90 -Env $envp
    }
    if ($r.ExitCode -eq 0) { Write-Good (Get-FirstLine $r.Output) }
    else {
        Write-Bad ('KiCad Python import check failed: ' + $r.Output)
        Write-Info 'If the message mentions pcbnew, the swig backend may not exist in this KiCad build. See SETUP.md, Troubleshooting.'
        Add-Result 'base MCP python' 'FAIL' (Get-FirstLine $r.Output)
    }
}

function Confirm-Companion {
    Write-Step 'Companion MCP (acd-mcp)'
    $pyproject = Join-Path $CompanionDir 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyproject)) {
        Write-Warn2 "No pyproject.toml in $CompanionDir yet. Skipping uv sync. Rerun this script once companion/ is in place."
        Add-Result 'companion' 'SKIPPED' 'companion/pyproject.toml not found'
        $script:CompanionReady = $false
        return
    }
    $uv = Get-ToolPath 'uv'
    Push-Location $CompanionDir
    try {
        & $uv sync
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code -ne 0) { throw "uv sync exit code $code" }
    Write-Good 'uv sync done'
    Add-Result 'companion' 'OK' 'uv sync done'
    $script:CompanionReady = $true
}

# ---------------------------------------------------------------------------
# Folders, library, environment variables
# ---------------------------------------------------------------------------
function Read-Folder([string]$Prompt, [string]$Given, [string]$EnvName, [string]$Default) {
    if ($Given) { return $Given }
    $existing = [Environment]::GetEnvironmentVariable($EnvName, 'User')
    if ($existing) { $Default = $existing }
    if ($NoPrompt) { return $Default }
    $ans = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($ans)) { return $Default }
    return $ans.Trim().Trim('"')
}

function Confirm-Folders {
    Write-Step 'Project and library folders'
    $dirs = @(
        $script:ProjRoot,
        $script:LibRoot,
        (Join-Path $script:LibRoot 'symbols'),
        (Join-Path $script:LibRoot 'footprints\acd.pretty'),
        (Join-Path $script:LibRoot '3dmodels')
    )
    foreach ($d in $dirs) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null; Write-Info "created $d" }
    }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $reg = Join-Path $script:LibRoot 'registry.json'
    if (-not (Test-Path -LiteralPath $reg)) {
        [System.IO.File]::WriteAllText($reg, "{}`n", $utf8); Write-Info "created $reg"
    }
    $sym = Join-Path $script:LibRoot 'symbols\acd.kicad_sym'
    if (-not (Test-Path -LiteralPath $sym)) {
        # Empty symbol library. Version 20241209 is the KiCad 9 library format; KiCad 10 opens it
        # and upgrades it the first time a symbol is saved.
        $text = "(kicad_symbol_lib`n`t(version 20241209)`n`t(generator `"acd_setup`")`n`t(generator_version `"10.0`")`n)`n"
        [System.IO.File]::WriteAllText($sym, $text, $utf8); Write-Info "created $sym"
    }
    Write-Good "projects: $($script:ProjRoot)"
    Write-Good "library:  $($script:LibRoot)"

    Set-UserEnv 'ACD_PROJECTS_ROOT' $script:ProjRoot
    Set-UserEnv 'ACD_LIBRARY_ROOT'  $script:LibRoot
    Set-UserEnv 'ACD_KICAD_CLI'     $script:KiCad.Cli
    Write-Good 'user environment variables set: ACD_PROJECTS_ROOT, ACD_LIBRARY_ROOT, ACD_KICAD_CLI, FREEROUTING_JAR'
    Add-Result 'folders and env vars' 'OK' ''
}

# ---------------------------------------------------------------------------
# Claude Desktop config merge
# ---------------------------------------------------------------------------
function Get-ClaudeConfigPath {
    if (Test-Path -LiteralPath $ClaudeDir) { return $ClaudeConfig }
    # Microsoft Store (MSIX) installs of Claude keep the config under Packages.
    $pk = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages') -Filter 'Claude_*' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pk) {
        $alt = Join-Path $pk.FullName 'LocalCache\Roaming\Claude'
        if (Test-Path -LiteralPath $alt) { return (Join-Path $alt 'claude_desktop_config.json') }
    }
    return $ClaudeConfig
}

function Merge-ClaudeConfig {
    Write-Step 'Claude Desktop config'
    $node = Get-ToolPath 'node'
    $uv   = Get-ToolPath 'uv'

    $kicadEnv = [ordered]@{
        PYTHONPATH      = $script:KiCad.DistPackages
        KICAD_PYTHON    = $script:KiCad.Python
        KICAD_BACKEND   = $Pins['base_mcp.backend']
        FREEROUTING_JAR = $script:FrJar
        NODE_ENV        = 'production'
        LOG_LEVEL       = 'info'
    }
    if (-not $script:JavaPathOk -and $script:JavaGood) {
        $javaBin = Split-Path -Parent $script:JavaGood.Exe
        $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $u = [Environment]::GetEnvironmentVariable('Path', 'User')
        $kicadEnv['PATH'] = [Environment]::ExpandEnvironmentVariables($javaBin + ';' + ((($m, $u) | Where-Object { $_ }) -join ';'))
    }
    $kicadEntry = [ordered]@{
        command = $node
        args    = @($script:McpIndex)
        env     = $kicadEnv
    }
    $acdEntry = [ordered]@{
        command = $uv
        args    = @('run', '--project', $CompanionDir, 'acd-mcp')
        env     = [ordered]@{
            ACD_PROJECTS_ROOT = $script:ProjRoot
            ACD_LIBRARY_ROOT  = $script:LibRoot
            ACD_KICAD_CLI     = $script:KiCad.Cli
            ACD_PROFILE       = $ProfilePath
        }
    }

    if ($SkipConfigMerge) { Write-Warn2 'Skipped (-SkipConfigMerge).'; Add-Result 'claude config' 'SKIPPED' ''; return }

    $cfgPath = Get-ClaudeConfigPath
    $cfgDir = Split-Path -Parent $cfgPath
    if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null }

    $root = $null
    $existed = Test-Path -LiteralPath $cfgPath
    if ($existed) {
        $raw = [System.IO.File]::ReadAllText($cfgPath)
        if ($raw.Trim() -ne '') {
            try { $root = $raw | ConvertFrom-Json }
            catch { throw "Existing $cfgPath is not valid JSON ($($_.Exception.Message)). Nothing was changed. Fix or move it, then rerun." }
        }
    }
    if (-not $root) { $root = [pscustomobject]@{} }
    if (-not ($root.PSObject.Properties.Name -contains 'mcpServers')) {
        $root | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([pscustomobject]@{})
    }
    $servers = $root.mcpServers
    if ($null -eq $servers) { $servers = [pscustomobject]@{}; $root.mcpServers = $servers }

    $changed = $false
    $entries = [ordered]@{ kicad = $kicadEntry; acd = $acdEntry }
    foreach ($name in $entries.Keys) {
        $newJson = $entries[$name] | ConvertTo-Json -Depth 10 -Compress
        $oldJson = $null
        if ($servers.PSObject.Properties.Name -contains $name) { $oldJson = $servers.$name | ConvertTo-Json -Depth 10 -Compress }
        if ($oldJson -eq $newJson) { Write-Good "entry '$name' already up to date"; continue }
        $obj = $newJson | ConvertFrom-Json
        $servers | Add-Member -NotePropertyName $name -NotePropertyValue $obj -Force
        if ($oldJson) { Write-Info "updated entry '$name'" } else { Write-Info "added entry '$name'" }
        $changed = $true
    }

    if (-not $changed) { Add-Result 'claude config' 'OK' 'no change'; return }

    if ($existed) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $bak = $cfgPath + '.acd-backup-' + $stamp
        Copy-Item -LiteralPath $cfgPath -Destination $bak
        Write-Good "backup: $bak"
    }
    $json = $root | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($cfgPath, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Good "wrote $cfgPath"
    Write-Warn2 'Fully quit Claude Desktop (tray icon, Quit) and reopen it to load the servers.'
    Add-Result 'claude config' 'UPDATED' $cfgPath
}

# ---------------------------------------------------------------------------
# Smoke check
# ---------------------------------------------------------------------------
function Test-Smoke {
    Write-Step 'Smoke check'
    $checks = New-Object System.Collections.ArrayList
    function Add-Check([string]$Name, [bool]$Pass, [string]$Detail) {
        [void]$checks.Add([pscustomobject]@{ Name = $Name; Pass = $Pass; Detail = $Detail })
        if ($Pass) { Write-Good "$Name : $Detail" } else { Write-Bad "$Name : $Detail"; $script:Failed = $true }
    }

    if ($script:KiCad) {
        $r = Invoke-Capture -File $script:KiCad.Cli -ArgList @('version') -TimeoutSec 60
        Add-Check 'kicad-cli version' ($r.ExitCode -eq 0 -and (Get-FirstLine $r.Output) -like '10.0*') (Get-FirstLine $r.Output)
    } else { Add-Check 'kicad-cli version' $false 'KiCad not found' }

    $jx = $null
    if ($script:JavaGood) { $jx = $script:JavaGood.Exe } elseif (Get-ToolPath 'java') { $jx = Get-ToolPath 'java' }
    if ($jx) {
        $r = Invoke-Capture -File $jx -ArgList @('-version') -TimeoutSec 30
        $line = ($r.Output -split "`r?`n" | Where-Object { $_ -match 'version' } | Select-Object -First 1)
        Add-Check 'java -version' ((Get-MajorVersion $line) -ge [int]$Pins['java.min_major']) $line
    } else { Add-Check 'java -version' $false 'java not found' }

    $nx = Get-ToolPath 'node'
    if ($nx) {
        $r = Invoke-Capture -File $nx -ArgList @('-v') -TimeoutSec 30
        Add-Check 'node -v' ((Get-MajorVersion $r.Output) -ge [int]$Pins['node.min_major']) (Get-FirstLine $r.Output)
    } else { Add-Check 'node -v' $false 'node not found' }

    $gx = Get-ToolPath 'git'
    if ($gx) { $r = Invoke-Capture -File $gx -ArgList @('--version'); Add-Check 'git --version' ($r.ExitCode -eq 0) (Get-FirstLine $r.Output) }
    else { Add-Check 'git --version' $false 'git not found' }

    $ux = Get-ToolPath 'uv'
    if ($ux) { $r = Invoke-Capture -File $ux -ArgList @('--version'); Add-Check 'uv --version' ($r.ExitCode -eq 0) (Get-FirstLine $r.Output) }
    else { Add-Check 'uv --version' $false 'uv not found' }

    Add-Check 'freerouting jar' ($script:FrJar -and (Test-Path -LiteralPath $script:FrJar)) ([string]$script:FrJar)
    Add-Check 'base MCP build' ($script:McpIndex -and (Test-Path -LiteralPath $script:McpIndex)) ([string]$script:McpIndex)

    if ($script:KiCad -and (Test-Path -LiteralPath $script:KiCad.Python)) {
        $r = Invoke-Capture -File $script:KiCad.Python -ArgList @('-c', "import pcbnew; print('pcbnew', pcbnew.GetBuildVersion())") -TimeoutSec 90 -Env @{ PYTHONPATH = $script:KiCad.DistPackages }
        Add-Check 'pcbnew import (swig backend)' ($r.ExitCode -eq 0) (Get-FirstLine $r.Output)
    }

    if ($script:CompanionReady -and $ux) {
        # Import check. "acd-mcp --help" is avoided on purpose: a stdio MCP server may wait for input.
        $r = Invoke-Capture -File $ux -ArgList @('run', '--project', $CompanionDir, 'python', '-c', "import acd, mcp, yaml; print('acd import ok')") -TimeoutSec 180
        Add-Check 'companion import (acd, mcp, yaml)' ($r.ExitCode -eq 0) (Get-FirstLine $r.Output)
        $exe = Join-Path $CompanionDir '.venv\Scripts\acd-mcp.exe'
        Add-Check 'acd-mcp entry point' (Test-Path -LiteralPath $exe) $exe
    } else {
        Write-Warn2 'Companion checks skipped (companion/ not ready).'
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host 'Auto Circuit Designer setup for Windows' -ForegroundColor White
Write-Host ("Pins checked on " + $Pins['checked'] + "; repo at $RepoRoot")
if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'PowerShell 5.1 or newer is required.' }
Update-SessionPath

$steps = @(
    @{ Name = 'git';          Fatal = $true;  Run = { Confirm-Git } },
    @{ Name = 'node';         Fatal = $true;  Run = { Confirm-Node } },
    @{ Name = 'java';         Fatal = $true;  Run = { Confirm-Java } },
    @{ Name = 'kicad';        Fatal = $true;  Run = { Confirm-KiCad } },
    @{ Name = 'uv';           Fatal = $true;  Run = { Confirm-Uv } },
    @{ Name = 'freerouting';  Fatal = $false; Run = { Confirm-Freerouting } },
    @{ Name = 'base MCP';     Fatal = $false; Run = { Confirm-BaseMcp } },
    @{ Name = 'companion';    Fatal = $false; Run = { Confirm-Companion } },
    @{ Name = 'folders';      Fatal = $false; Run = { Confirm-Folders } },
    @{ Name = 'claude config'; Fatal = $false; Run = { Merge-ClaudeConfig } }
)
$script:KiCad = $null; $script:JavaGood = $null; $script:JavaPathOk = $true
$script:FrJar = $null; $script:McpIndex = $null; $script:CompanionReady = $false

# Ask the questions first so the long installs can run unattended afterwards.
$defaultBase = Join-Path $env:USERPROFILE 'Documents\AutoCircuitDesigner'
$script:ProjRoot = Read-Folder 'Folder for PCB projects (ACD_PROJECTS_ROOT)' $ProjectsRoot 'ACD_PROJECTS_ROOT' (Join-Path $defaultBase 'Projects')
$script:LibRoot  = Read-Folder 'Folder for the component library (ACD_LIBRARY_ROOT)' $LibraryRoot 'ACD_LIBRARY_ROOT' (Join-Path $defaultBase 'Library')

foreach ($s in $steps) {
    # Later steps need the earlier tools; skip them cleanly if those are missing.
    $needsKiCad = @('base MCP', 'folders', 'claude config') -contains $s.Name
    if ($needsKiCad -and -not $script:KiCad) { Write-Warn2 "Skipping '$($s.Name)': KiCad is not available."; Add-Result $s.Name 'SKIPPED' 'KiCad missing'; continue }
    if ($s.Name -eq 'claude config' -and (-not $script:McpIndex -or -not $script:FrJar)) {
        Write-Warn2 "Skipping 'claude config': an earlier step did not finish, so the entries would be wrong."
        Add-Result 'claude config' 'SKIPPED' 'earlier step failed'; continue
    }
    if ($s.Name -eq 'base MCP' -and (-not (Get-ToolPath 'git') -or -not (Get-ToolPath 'node'))) {
        Write-Warn2 "Skipping 'base MCP': git or node is missing."; Add-Result 'base MCP' 'SKIPPED' 'git or node missing'; continue
    }
    if ($s.Name -eq 'companion' -and -not (Get-ToolPath 'uv')) {
        Write-Warn2 "Skipping 'companion': uv is missing."; Add-Result 'companion' 'SKIPPED' 'uv missing'; continue
    }
    try { & $s.Run }
    catch {
        Write-Bad $_.Exception.Message
        Add-Result $s.Name 'FAIL' $_.Exception.Message
    }
}

try { Test-Smoke } catch { Write-Bad $_.Exception.Message; $script:Failed = $true }

Write-Step 'Summary'
$script:Results | Format-Table -AutoSize Item, Status, Detail | Out-String -Width 200 | Write-Host
if ($script:Failed) {
    Write-Host 'Finished with problems. Fix the [FAIL] lines above and rerun this script (it skips finished steps).' -ForegroundColor Red
    exit 1
}
Write-Host 'All done. Fully quit and reopen Claude Desktop, then see SETUP.md, "First test".' -ForegroundColor Green
exit 0
