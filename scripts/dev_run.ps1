# Start the backend and the frontend together and probe both.
#
# The frontend is served from the scratch copy in %TEMP% (see
# build_frontend.ps1 for why the in-workspace node_modules cannot be used).
# Both processes are started detached so they survive independently of whatever
# terminal issued this script.

$ErrorActionPreference = 'Continue'

# The port is a parameter rather than a literal, resolved the same way
# `restart_backend.ps1` resolves it: -Port, then $env:BACKEND_PORT, then 8001.
# It used to be a hardcoded 8000 in two places, which meant this script could
# never succeed here — 8000 is permanently reserved by a zombie socket on this
# machine (see docs/env/ports.md), so it both cleared the wrong thing and started
# something that could not bind.
param(
    [int]$Port = 0,
    [int]$FrontendPort = 5173
)

if ($Port -eq 0) {
    if ($env:BACKEND_PORT) { $Port = [int]$env:BACKEND_PORT } else { $Port = 8001 }
}

$root = 'h:\agent_scaffold'
$web = Join-Path $env:TEMP 'agent_web'
$python = 'd:\AI_LLM_Project\venv_d\Scripts\python.exe'
$report = Join-Path $env:TEMP 'agent_dev_report.txt'

function Note([string]$text) {
    Add-Content -Path $report -Value $text -Encoding utf8
}

Set-Content -Path $report -Value "START $(Get-Date -Format 'HH:mm:ss')" -Encoding utf8

# --- 0. Clear anything already listening on the two ports -------------------
foreach ($port in $Port, $FrontendPort) {
    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owner in $owners) {
        if ($owner -and $owner -ne 0) {
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
            Note "KILLED_PORT_${port}_PID=$owner"
        }
    }
}

# --- 1. Backend -------------------------------------------------------------
$backendOut = Join-Path $env:TEMP 'agent_backend.log'
$backendErr = Join-Path $env:TEMP 'agent_backend.err'

$backend = Start-Process -FilePath $python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port" `
    -WorkingDirectory (Join-Path $root 'backend') `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

Note "BACKEND_PID=$($backend.Id)"

# --- 2. Frontend ------------------------------------------------------------
$viteOut = Join-Path $env:TEMP 'agent_vite.log'
$viteErr = Join-Path $env:TEMP 'agent_vite.err'

$frontend = Start-Process -FilePath 'F:\Node.s\npm.cmd' `
    -ArgumentList 'run', 'dev', '--', '--host', '127.0.0.1' `
    -WorkingDirectory $web `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $viteOut -RedirectStandardError $viteErr

Note "FRONTEND_PID=$($frontend.Id)"

# --- 3. Probe ---------------------------------------------------------------
# The backend imports the whole node registry at startup, so give it a moment
# before probing; a failure here is a real startup error, not impatience.
function Wait-For([string]$url, [int]$attempts = 40) {
    for ($i = 1; $i -le $attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            return "OK=$($response.StatusCode)"
        } catch {
            Start-Sleep -Milliseconds 750
        }
    }
    return 'FAILED'
}

Note "BACKEND_ALIVE=$([bool](Get-Process -Id $backend.Id -ErrorAction SilentlyContinue))"
Note "HEALTH=$(Wait-For "http://127.0.0.1:$Port/api/node-types")"
Note "NODE_TYPES_STATUS=$(Wait-For "http://127.0.0.1:$Port/api/ai/status")"
Note "FRONTEND_ALIVE=$([bool](Get-Process -Id $frontend.Id -ErrorAction SilentlyContinue))"
Note "VITE=$(Wait-For "http://127.0.0.1:$FrontendPort/")"
Note "VITE_PROXY=$(Wait-For "http://127.0.0.1:$FrontendPort/api/node-types")"

Note "DONE $(Get-Date -Format 'HH:mm:ss')"
