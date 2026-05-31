<#
.SYNOPSIS
    SBV2 (Style-Bert-VITS2) /voice watchdog — auto-restarts the TTS server on the
    main PC when it hangs, so Pico's autonomous speech (Phase X) stays reliable.

.DESCRIPTION
    Polls the SBV2 /voice endpoint every WATCHDOG_INTERVAL_SEC. If the endpoint is
    degraded (HTTP 500 / read or connect timeout / connection refused) it restarts
    SBV2 (kill the process listening on WATCHDOG_PORT, then run WATCHDOG_RESTART_CMD),
    waits for the model to load, and re-probes. Every transition is appended to
    WATCHDOG_LOG_PATH. After WATCHDOG_RESTART_MAX consecutive failed recoveries it
    logs an ERROR-level alert and keeps trying on the normal interval.

    Runs on Windows PowerShell 5.1 and PowerShell 7+. No external modules required.

.NOTES
    Config precedence: environment variable > config file > built-in default.
    Config file is a simple KEY=VALUE text file (see sbv2_watchdog.conf.example).
    Authored on the Pi5; deploy + test on the main PC per README.md (this host
    cannot exercise Windows process control).
#>

[CmdletBinding()]
param(
    # Path to the KEY=VALUE config file. Defaults to sbv2_watchdog.conf next to this script.
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'sbv2_watchdog.conf'),
    # Probe once, log the verdict, and exit (used by the README test/CI). No restart.
    [switch]$Once,
    # Run the restart routine immediately regardless of health (used to test recovery).
    [switch]$ForceRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Defaults (overridden by config file, then by environment variables) ────────
$Defaults = @{
    WATCHDOG_INTERVAL_SEC      = 300
    WATCHDOG_TIMEOUT_SEC       = 30
    WATCHDOG_TARGET_URL        = 'http://localhost:5000/voice?text=ping&model_name=jvnv-F1-jp&speaker_id=0&style_weight=0.0'
    WATCHDOG_PORT              = 5000
    WATCHDOG_RESTART_MAX       = 3
    WATCHDOG_RESTART_CMD       = 'D:\Pico\StyleBertVITS2\start_sbv2.bat'  # ← set to your real launcher
    WATCHDOG_MODEL_LOAD_WAIT_SEC = 90
    WATCHDOG_POST_RESTART_PROBE_TIMEOUT_SEC = 30
    WATCHDOG_POST_RESTART_PROBE_RETRIES = 6   # poll every 10s up to retries*10s after load wait
    WATCHDOG_LOG_PATH          = 'D:\Pico\sbv2_watchdog.log'
}

function Read-Config {
    param([string]$Path, [hashtable]$Defaults)
    $cfg = $Defaults.Clone()
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $kv = $t -split '=', 2
            if ($kv.Count -eq 2) {
                $key = $kv[0].Trim()
                $val = $kv[1].Trim()
                if ($cfg.ContainsKey($key)) { $cfg[$key] = $val } else { $cfg[$key] = $val }
            }
        }
    }
    # Environment variables win (so the Task Scheduler / shell can override without editing the file).
    foreach ($key in @($cfg.Keys)) {
        $envVal = [Environment]::GetEnvironmentVariable($key)
        if ($null -ne $envVal -and $envVal -ne '') { $cfg[$key] = $envVal }
    }
    return $cfg
}

function Write-WatchdogLog {
    param([string]$Level, [string]$Message, [string]$LogPath)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "$ts [$Level] $Message"
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch {
        # Never let logging failure crash the watchdog.
    }
    Write-Host $line
}

# Returns @{ Healthy = $bool; Reason = 'ok'|'http_500'|'timeout'|'refused'|'error:...' }
function Test-Sbv2Health {
    param([string]$Url, [int]$TimeoutSec)
    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        if ([int]$resp.StatusCode -eq 200) { return @{ Healthy = $true;  Reason = 'ok' } }
        return @{ Healthy = $false; Reason = "http_$([int]$resp.StatusCode)" }
    } catch {
        $ex = $_.Exception
        # Non-2xx: PS7 throws HttpResponseException; PS5.1 throws WebException — both expose .Response.
        $resp = $null
        try { $resp = $ex.Response } catch { $resp = $null }
        if ($null -ne $resp) {
            try {
                $code = [int]$resp.StatusCode
                if ($code -gt 0) { return @{ Healthy = $false; Reason = "http_$code" } }
            } catch { }
        }
        $msg = "$($ex.Message)"
        if ($msg -match '(?i)time(d)?\s*out')                              { return @{ Healthy = $false; Reason = 'timeout' } }
        if ($msg -match '(?i)refused|unable to connect|No connection|target machine actively refused') {
            return @{ Healthy = $false; Reason = 'refused' }
        }
        return @{ Healthy = $false; Reason = "error:$msg" }
    }
}

function Stop-Sbv2 {
    param([int]$Port, [string]$LogPath)
    $killed = $false
    try {
        $pids = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            if ($procId -and $procId -ne 0) {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-WatchdogLog 'INFO' "stopped SBV2 pid=$procId on port $Port" $LogPath
                $killed = $true
            }
        }
    } catch {
        Write-WatchdogLog 'WARN' "Stop-Sbv2 failed: $($_.Exception.Message)" $LogPath
    }
    if (-not $killed) {
        Write-WatchdogLog 'INFO' "no listener on port $Port (process already down)" $LogPath
    }
    Start-Sleep -Seconds 2
}

function Start-Sbv2 {
    param([string]$RestartCmd, [string]$LogPath)
    if (-not (Test-Path -LiteralPath $RestartCmd)) {
        Write-WatchdogLog 'ERROR' "RESTART_CMD not found: $RestartCmd (set WATCHDOG_RESTART_CMD)" $LogPath
        return $false
    }
    try {
        # Launch detached so the watchdog does not block on the server process.
        Start-Process -FilePath $RestartCmd -WindowStyle Minimized | Out-Null
        Write-WatchdogLog 'INFO' "launched SBV2 via $RestartCmd" $LogPath
        return $true
    } catch {
        Write-WatchdogLog 'ERROR' "Start-Sbv2 failed: $($_.Exception.Message)" $LogPath
        return $false
    }
}

# Full recovery cycle. Returns $true if SBV2 is healthy afterwards.
function Invoke-Recovery {
    param([hashtable]$Cfg)
    $log = $Cfg.WATCHDOG_LOG_PATH
    $startedAt = Get-Date

    Stop-Sbv2 -Port ([int]$Cfg.WATCHDOG_PORT) -LogPath $log
    if (-not (Start-Sbv2 -RestartCmd $Cfg.WATCHDOG_RESTART_CMD -LogPath $log)) {
        return $false
    }

    Write-WatchdogLog 'INFO' "waiting $($Cfg.WATCHDOG_MODEL_LOAD_WAIT_SEC)s for model load" $log
    Start-Sleep -Seconds ([int]$Cfg.WATCHDOG_MODEL_LOAD_WAIT_SEC)

    $retries = [int]$Cfg.WATCHDOG_POST_RESTART_PROBE_RETRIES
    $probeTimeout = [int]$Cfg.WATCHDOG_POST_RESTART_PROBE_TIMEOUT_SEC
    for ($i = 1; $i -le $retries; $i++) {
        $h = Test-Sbv2Health -Url $Cfg.WATCHDOG_TARGET_URL -TimeoutSec $probeTimeout
        if ($h.Healthy) {
            $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
            Write-WatchdogLog 'INFO' "RECOVERED in ${elapsed}s (probe $i/$retries)" $log
            return $true
        }
        Write-WatchdogLog 'INFO' "post-restart probe $i/$retries not ready ($($h.Reason))" $log
        Start-Sleep -Seconds 10
    }
    Write-WatchdogLog 'WARN' "post-restart probes exhausted, SBV2 still unhealthy" $log
    return $false
}

# ── Main ───────────────────────────────────────────────────────────────────────
$cfg = Read-Config -Path $ConfigPath -Defaults $Defaults
$log = $cfg.WATCHDOG_LOG_PATH

Write-WatchdogLog 'INFO' ("watchdog start (interval={0}s timeout={1}s target={2})" -f `
    $cfg.WATCHDOG_INTERVAL_SEC, $cfg.WATCHDOG_TIMEOUT_SEC, $cfg.WATCHDOG_TARGET_URL) $log

if ($ForceRestart) {
    Write-WatchdogLog 'INFO' 'ForceRestart requested (test mode)' $log
    $ok = Invoke-Recovery -Cfg $cfg
    if (-not $ok) { exit 1 }
    exit 0
}

$consecutiveFailures = 0

do {
    $health = Test-Sbv2Health -Url $cfg.WATCHDOG_TARGET_URL -TimeoutSec ([int]$cfg.WATCHDOG_TIMEOUT_SEC)

    if ($health.Healthy) {
        if ($consecutiveFailures -gt 0) {
            Write-WatchdogLog 'INFO' "healthy again after $consecutiveFailures failed cycle(s)" $log
        }
        $consecutiveFailures = 0
        Write-Verbose 'SBV2 healthy'
    } else {
        Write-WatchdogLog 'WARN' "SBV2 degraded: $($health.Reason) — initiating recovery" $log
        $recovered = Invoke-Recovery -Cfg $cfg
        if ($recovered) {
            $consecutiveFailures = 0
        } else {
            $consecutiveFailures++
            Write-WatchdogLog 'WARN' "recovery attempt failed ($consecutiveFailures/$($cfg.WATCHDOG_RESTART_MAX))" $log
            if ($consecutiveFailures -ge [int]$cfg.WATCHDOG_RESTART_MAX) {
                Write-WatchdogLog 'ERROR' ("ALERT: SBV2 not recovered after {0} consecutive attempts — manual intervention needed" -f $consecutiveFailures) $log
            }
        }
    }

    if ($Once) { break }
    Start-Sleep -Seconds ([int]$cfg.WATCHDOG_INTERVAL_SEC)
} while ($true)
