# SBV2 /voice Watchdog (main PC, Windows)

Auto-restarts the Style-Bert-VITS2 TTS server (`:5000`) when it hangs, so Pico's
autonomous speech (Phase X) survives the SBV2 instability seen on 2026-05-31
(HTTP 500 at 11:05; TCP timeout at ~14:00 — both fixed by a manual restart).

> **Authoring note:** these files were written on the Pi5 (Linux) and **cannot be
> run or tested from there** — they control a Windows process, Task Scheduler, and
> `D:\Pico\`. Deploy and run the tests below **on the main PC**. The watchdog logic
> is parameterized; the only required edit is `WATCHDOG_RESTART_CMD`.

## Files

| File | Purpose |
|------|---------|
| `sbv2_watchdog.ps1` | The watchdog loop (PS 5.1 / 7+, no modules). |
| `sbv2_watchdog.conf.example` | Config template → copy to `sbv2_watchdog.conf`. |
| `SBV2Watchdog.task.xml` | Task Scheduler definition (logon trigger, highest privilege). |

## 1. Deploy

```powershell
# On the main PC, e.g. copy this folder to D:\Pico\sbv2_watchdog\
Copy-Item -Recurse <repo>\deploy\windows\sbv2_watchdog D:\Pico\sbv2_watchdog
cd D:\Pico\sbv2_watchdog
Copy-Item sbv2_watchdog.conf.example sbv2_watchdog.conf
notepad sbv2_watchdog.conf   # *** set WATCHDOG_RESTART_CMD to your real SBV2 launcher ***
```

`WATCHDOG_RESTART_CMD` must point at whatever you currently use to start SBV2
(e.g. a `start_sbv2.bat` that activates the venv/conda env and launches the
server). The watchdog kills the process listening on `WATCHDOG_PORT` and then
runs this command — so it should be self-contained (launch and detach).

## 2. Smoke test (foreground, no scheduler yet)

```powershell
# One probe + verdict, no restart:
powershell -NoProfile -ExecutionPolicy Bypass -File .\sbv2_watchdog.ps1 -Once
# Expect: "... [INFO] ... healthy" in console + D:\Pico\sbv2_watchdog.log
```

If `-Once` reports `degraded: refused`/`timeout` while SBV2 is actually up, check
`WATCHDOG_TARGET_URL`/`WATCHDOG_PORT` and that the URL returns 200 in a browser.

## 3. Recovery test — completion conditions #2 and #4

**a) Process-down (TCP refused/timeout) — simulates the ~14:00 hang:**
```powershell
# Kill SBV2 (Task Manager → end task, or by port):
Get-NetTCPConnection -LocalPort 5000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
# Drive one recovery cycle and watch it relaunch + re-probe:
powershell -NoProfile -ExecutionPolicy Bypass -File .\sbv2_watchdog.ps1 -ForceRestart
# Expect log: stopped/no-listener → launched → waiting Ns → RECOVERED in Ns
```
(`-ForceRestart` runs the stop→start→verify cycle once and exits 0 on success,
1 on failure — handy for scripted verification.)

**b) HTTP 500 (server alive, /voice failing) — simulates the 11:05 hang:**
There is no clean way to force a 500 without touching SBV2. Easiest proxy: point
`WATCHDOG_TARGET_URL` at a path SBV2 answers with non-200 (e.g. a bogus
`model_name`) and run `-Once`; confirm the verdict is `http_500`/`http_4xx` and
that a full run triggers recovery. Then restore the real URL. Alternatively, if
SBV2 has a debug/maintenance toggle, use it to induce a 500.

**c) Recovery-failure alert (#5):** temporarily set `WATCHDOG_RESTART_CMD` to a
path that does **not** start a server and `WATCHDOG_RESTART_MAX=2`; run the loop
and confirm an `ERROR ... ALERT: SBV2 not recovered after N consecutive attempts`
line appears. Restore the real command afterwards.

## 4. Register with Task Scheduler — completion condition #3

**Option A — Register-ScheduledTask (no XML editing, recommended):**
```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\Pico\sbv2_watchdog\sbv2_watchdog.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
  -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 3 -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName 'SBV2Watchdog' -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force
```

**Option B — import the XML:** edit `SBV2Watchdog.task.xml` (`<UserId>` and the
`-File` path), then:
```powershell
schtasks /create /tn "SBV2Watchdog" /xml "D:\Pico\sbv2_watchdog\SBV2Watchdog.task.xml" /ru "<USER>" /rp "<PASSWORD>"
```

Verify / start now:
```powershell
Get-ScheduledTask -TaskName SBV2Watchdog
Start-ScheduledTask -TaskName SBV2Watchdog   # start without waiting for logon
```

> The watchdog must run in the **same desktop/GPU session** as SBV2. Use
> `LogonType Interactive` (above), not a service account, so the relaunched SBV2
> sees the GPU. `-AtLogOn` covers Pico's normal "log in, everything starts" flow.

## 5. Log — completion condition #5

`D:\Pico\sbv2_watchdog.log` (configurable). Lines:
```
2026-05-31 14:05:00 [WARN]  SBV2 degraded: timeout — initiating recovery
2026-05-31 14:05:02 [INFO]  stopped SBV2 pid=12345 on port 5000
2026-05-31 14:05:04 [INFO]  launched SBV2 via D:\Pico\StyleBertVITS2\start_sbv2.bat
2026-05-31 14:05:04 [INFO]  waiting 90s for model load
2026-05-31 14:06:40 [INFO]  RECOVERED in 100s (probe 1/6)
```
Tail it: `Get-Content D:\Pico\sbv2_watchdog.log -Wait -Tail 20`

## 6. Root-cause investigation (optional, run when a hang recurs)

The watchdog keeps Pico running, but the underlying SBV2 instability is worth
pinning down. The 11:05 (HTTP 500, process alive) vs ~14:00 (TCP timeout, process
unresponsive) difference hints at two modes: a per-request failure vs a whole-
process stall (often GPU/CUDA-context or VRAM exhaustion).

- **Continuous VRAM/GPU log** (leave running, correlate timestamps with the
  watchdog log): `nvidia-smi -l 60 > D:\Pico\nvidia_smi.log` — or richer:
  `nvidia-smi --query-gpu=timestamp,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv -l 60 > D:\Pico\gpu.csv`
  Look for `memory.used` creeping up toward `memory.total` before a hang (leak/OOM).
- **SBV2 server log:** capture stdout/stderr of the launcher to a file
  (`... > D:\Pico\sbv2_server.log 2>&1` in the start bat) and check for CUDA
  errors / OOM / tracebacks at the hang time. If SBV2 has a verbose/debug flag,
  enable it.
- **Mode correlation:** if a 500 coincides with high but not full VRAM →
  per-request failure (e.g. a bad input / transient model error). If a TCP
  timeout coincides with VRAM at/near max or a CUDA error in the server log →
  process-level stall (GPU OOM / context loss) — the restart is the right fix and
  a VRAM headroom / periodic-restart policy may help long-term.

If root cause stays elusive, the watchdog alone is sufficient for operation.

## Tuning notes

- `WATCHDOG_MODEL_LOAD_WAIT_SEC=90` assumes ~60–120s model load. If your cold
  start is faster/slower, adjust; post-restart probing retries 6×10s on top.
- A healthy warm `/voice` answers in a few seconds; cold start ~17.5s was observed
  historically — that's why `WATCHDOG_TIMEOUT_SEC=30` (steady-state probe) is
  generous and the *post-restart* probe timeout is separate.
- Interval 300s means a hang is detected within ≤5 min. Lower it if Pico's speech
  cadence makes 5 min too long; raising it reduces idle GPU pokes.
