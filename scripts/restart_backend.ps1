# Restart the backend, replacing whatever already holds the port.
#
# Exists because killing the old server by PID kept silently failing, and
# because a stale process keeps serving the previous code — the catalogue and
# validation messages are localised, so "it still answers in English" is
# usually a liveness problem rather than a translation one.
#
# The port is a parameter rather than a literal. Resolution order is -Port,
# then $env:BACKEND_PORT, then 8001 — not 8000, because 8000 is permanently
  # reserved on this machine by a zombie socket (see docs/env/ports.md) and a
# script that defaults to it can never succeed.
#
# Outcome is written to _restart.txt rather than printed: the console here
# mangles UTF-8, and the Chinese labels are exactly what needs verifying.

[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoVerify
)

$ErrorActionPreference = 'Continue'

if ($Port -le 0) {
    if ($env:BACKEND_PORT) { $Port = [int]$env:BACKEND_PORT } else { $Port = 8001 }
}

$root = 'h:\agent_scaffold\backend'
$report = Join-Path $root '_restart.txt'
$python = 'd:\AI_LLM_Project\venv_d\Scripts\python.exe'

function Note([string]$text) {
    Add-Content -Path $report -Value $text -Encoding utf8
}

# Progress goes to the terminal *and* the report.
#
# `Write-Host` rather than `Write-Output`, and that is the point: Write-Output
# writes to the success stream, which a caller's `| Select-Object` buffers until
# the pipeline closes — so the progress would appear only at the end, which is
# exactly when it is useless. Write-Host goes straight to the console.
#
# The report file is kept because grepping it beats scrolling a terminal, and
# because a detached run has no terminal at all.
function Step([string]$text) {
    Write-Host "[restart] $text"
    Note $text
}

Set-Content -Path $report -Value "START $(Get-Date -Format 'HH:mm:ss') PORT=$Port" -Encoding utf8
Step "端口 $Port · $(Get-Date -Format 'HH:mm:ss')"

# --- 1. Free the port -------------------------------------------------------
# `Get-NetTCPConnection` reported a PID that `Get-Process` did not know about,
# so the raw netstat table is used instead: it is the same source the kernel
# uses to decide that the address is still in use.
$pattern = ":$Port\s"
$owners = @()
foreach ($line in (netstat -ano | Select-String $pattern)) {
    if ($line -match 'LISTENING\s+(\d+)\s*$') {
        $owners += $Matches[1]
    }
}
$owners = $owners | Sort-Object -Unique
Note "OWNERS=$($owners -join ',')"
if ($owners.Count -gt 0) {
    Step "  占用进程 $($owners -join ', ') → 终止"
}

foreach ($owner in $owners) {
    & taskkill /F /PID $owner 2>&1 | Out-Null
    Note "KILL_EXIT_${owner}=$LASTEXITCODE"
}

Start-Sleep -Seconds 2

$remaining = @()
foreach ($line in (netstat -ano | Select-String $pattern)) {
    if ($line -match 'LISTENING\s+(\d+)\s*$') {
        $remaining += $Matches[1]
    }
}
Note "REMAINING=$($remaining -join ',')"

if ($remaining.Count -gt 0) {
    Step "  端口仍被占用：$($remaining -join ', ') — 疑似僵尸 socket（docs/env/ports.md）"
    Note "DONE $(Get-Date -Format 'HH:mm:ss')"
    exit 1
}
Step "  端口已释放"

# --- 2. Start it again ------------------------------------------------------
# Two requirements pull in opposite directions, and getting both took measuring
# rather than reasoning — three arrangements each satisfied one and broke the
# other:
#
#   (a) it must outlive this script — so it cannot be a plain child;
#   (b) it must not hold this script's stdout — otherwise anyone who pipes the
#       script (`... | Select-Object -Last 5`) gets a command that never
#       returns. The pipe cannot reach EOF while a live process holds the write
#       end. The script had finished in ten seconds; the shell waited forever.
#
#   1. Start-Process + -RedirectStandardOutput/-Error
#      -> (a) yes, (b) no. Windows children inherit *handles*; redirecting the
#         stream object does not stop the child from inheriting the pipe.
#   2. Win32_Process.Create + a launcher .cmd
#      -> (b) yes, (a) no. cmd stays as the parent and the whole chain dies when
#         this script exits.
#   3. ... + `start /b`
#      -> still (a) no. /b shares cmd's console, so destroying it kills the child.
#
# What works: Start-Process on cmd.exe, and let *cmd* do the redirection. The
# child then receives file handles and never touches our pipe, while
# -WindowStyle Hidden keeps it detached. Measured with a minimal probe: the
# pipeline returned in 1s and the server was still listening afterwards.
#
# The command line goes through a launcher file rather than an inline string.
# `cmd /c` with a command line that starts with a quote and contains several more
# applies a quote-stripping heuristic, and the inline version failed with
# "此时不应有 >" — a syntax error from cmd's own parser, not from the redirect.
# One `cmd /c "<file>"` has a single quote pair for cmd to interpret.
#
# The launcher is written as ASCII, never UTF-8: `cmd.exe` executes a UTF-8 BOM
# as part of the first command and fails on it.
$outLog = Join-Path $root '_out.log'
$errLog = Join-Path $root '_err.log'
$launcher = Join-Path $root '_launch.cmd'

@(
    '@echo off',
    "`"$python`" -m uvicorn app.main:app --host 127.0.0.1 --port $Port > `"$outLog`" 2> `"$errLog`""
) | Set-Content -Path $launcher -Encoding ascii

$child = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "`"$launcher`"" `
    -WorkingDirectory $root `
    -WindowStyle Hidden -PassThru

Step "  已启动（cmd PID=$($child.Id)，日志 backend\_err.log）"

# --- 3. Wait for the port, rather than guessing how long it needs ------------
# A fixed sleep is wrong in both directions: it wastes time when the server is
# quick and it produces a bogus "connection refused" when the server is slow.
# Polling reports the thing we actually care about, and it is also the only
# reliable liveness signal now that the launcher detaches — there is no child
# handle to ask.
Step "  等待端口 $Port 就绪"
$deadline = (Get-Date).AddSeconds(60)
$listening = $false
$backendPid = ''
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $onPort = @()
    foreach ($line in (netstat -ano | Select-String ":$Port\s")) {
        if ($line -match 'LISTENING\s+(\d+)\s*$') { $onPort += $Matches[1] }
    }
    if ($onPort.Count -gt 0) {
        $listening = $true
        $backendPid = $onPort[0]
        break
    }
}

if (-not $listening) {
    Step "  端口 $Port 在 60 秒内没有就绪 — 启动失败"
    if (Test-Path $errLog) {
        $tail = Get-Content $errLog -Encoding utf8 -Tail 15 -ErrorAction SilentlyContinue
        foreach ($line in $tail) {
            if ("$line".Trim()) { Write-Host "[restart]     $line"; Note "    $line" }
        }
    }
    Note "DONE $(Get-Date -Format 'HH:mm:ss')"
    exit 1
}

Step "  已就绪，PID=$backendPid"

if ($NoVerify) {
    Step "  已跳过自检（-NoVerify）"
    Note "DONE $(Get-Date -Format 'HH:mm:ss')"
    exit 0
}

# --- 3. Verify the API is localised -----------------------------------------
# Written with Python rather than Invoke-RestMethod: the response is UTF-8 JSON
# and PowerShell's default decoding mangles the Chinese. The probe covers both
# surfaces that used to be English-only — the catalogue and the validator.
$probe = Join-Path $root '_verify.py'
@"
import json, urllib.request

base = 'http://127.0.0.1:$Port'
out = {}
try:
    d = json.load(urllib.request.urlopen(base + '/api/node-types?lang=zh', timeout=10))
    by_type = {t['type']: t for t in d['types']}
    out['catalogue'] = {
        'lang': d.get('lang'),
        'start_label': by_type['start']['label'],
        'llm_label': by_type['llm']['label'],
        'has_zh_field': any(k.endswith('_zh') for t in d['types'] for k in t),
    }
except Exception as exc:
    out['catalogue'] = {'error': f'{type(exc).__name__}: {exc}'}

try:
    body = json.dumps({
        'id': 'probe', 'name': 'probe',
        'nodes': [{'id': 'n1', 'type': 'llm', 'label': 'LLM',
                   'position': {'x': 0, 'y': 0}, 'config': {'prompt': ''}}],
        'edges': [],
    }).encode()
    req = urllib.request.Request(base + '/api/workflows/validate?lang=zh', data=body,
                                 headers={'Content-Type': 'application/json'})
    r = json.load(urllib.request.urlopen(req, timeout=10))
    out['validation'] = {
        'issue_count': len(r['issues']),
        'messages': [i['message'] for i in r['issues']],
        'has_zh_field': any(k.endswith('_zh') for i in r['issues'] for k in i),
    }
except Exception as exc:
    out['validation'] = {'error': f'{type(exc).__name__}: {exc}'}

open(r'$root\i18n_api.json', 'w', encoding='utf-8').write(
    json.dumps(out, ensure_ascii=False, indent=1))

# Printed for the terminal. Python does the printing because the JSON holds
# Chinese and keeping the decoding in one process avoids the console mangling it
# on the way through PowerShell.
cat = out['catalogue']
if 'error' in cat:
    print('FAIL  catalogue: ' + cat['error'])
else:
    leak = 'LEAKED' if cat['has_zh_field'] else 'ok'
    print('OK    catalogue: lang=' + str(cat['lang'])
          + ' start=' + cat['start_label']
          + ' llm=' + cat['llm_label']
          + ' zh_fields=' + leak)

val = out['validation']
if 'error' in val:
    print('FAIL  validation: ' + val['error'])
else:
    leak = 'LEAKED' if val['has_zh_field'] else 'ok'
    print('OK    validation: ' + str(val['issue_count']) + ' issue(s), zh_fields=' + leak)
    for message in val['messages']:
        print('        ' + message)
"@ | Set-Content -Path $probe -Encoding utf8

Step "自检：目录与校验两处本地化"
$verdict = & $python $probe 2>&1
foreach ($line in $verdict) {
    $text = [string]$line
    if ($text.Trim()) {
        Write-Host "[restart]   $text"
        Note $text
    }
}
$bad = @($verdict | Where-Object { ([string]$_).StartsWith('FAIL') })
if ($bad.Count -gt 0) {
    Step "自检未通过 — 看上面的 FAIL 行"
    Note "DONE $(Get-Date -Format 'HH:mm:ss')"
    exit 1
}

Step "完成（$(Get-Date -Format 'HH:mm:ss')）"
exit 0
