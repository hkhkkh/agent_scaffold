# Build the frontend in a scratch directory outside the workspace.
#
# Two environmental problems made installing in-place impossible, and this
# script exists only to route around them:
#
#  1. `frontend/node_modules` is stuck in a Windows *delete-pending* state. It
#     exists with normal permissions, but listing, renaming, or deleting it
#     fails with WinError 5 because a handle is still open (VS Code's file
#     watcher is the likely holder — no node process is involved). Any tool that
#     tries to clear it first hangs.
#  2. pnpm prepares each package in a `<name>_tmp_<id>` directory and then
#     renames it into place. That rename fails with ERR_PNPM_EPERM when the
#     destination already exists, which is exactly the situation the wedged
#     directory creates. npm unpacks directly instead and does not hit this.
#
# So: copy the sources to %TEMP%, install with npm, and run the type checker
# there. Nothing in the workspace is deleted or overwritten.

$ErrorActionPreference = 'Continue'

$src = 'h:\agent_scaffold\frontend'
$dst = Join-Path $env:TEMP 'agent_web'
$report = Join-Path $env:TEMP 'agent_web_report.txt'

function Note([string]$text) {
    Add-Content -Path $report -Value $text -Encoding utf8
}

Set-Content -Path $report -Value "START $(Get-Date -Format 'HH:mm:ss')" -Encoding utf8
Note "SCRATCH=$dst"

# --- 1. Fresh scratch directory -------------------------------------------
# Removed with cmd's rd rather than Remove-Item: rd does not enumerate the tree
# before deleting, so a stale handle deep inside cannot stall it.
if (Test-Path $dst) {
    cmd /c "rd /s /q `"$dst`"" 2>&1 | Out-Null
}
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Note "SCRATCH_CREATED=$(Test-Path $dst)"

# --- 2. Copy sources and config -------------------------------------------
Copy-Item (Join-Path $src 'src') $dst -Recurse -Force -ErrorAction SilentlyContinue
foreach ($file in 'package.json', 'tsconfig.json', 'vite.config.ts', 'index.html') {
    Copy-Item (Join-Path $src $file) $dst -Force -ErrorAction SilentlyContinue
}
Note "SRC_FILES=$((Get-ChildItem (Join-Path $dst 'src') -Recurse -File -ErrorAction SilentlyContinue).Count)"
Note "HAS_PACKAGE=$(Test-Path (Join-Path $dst 'package.json'))"
Note "COPY_DONE $(Get-Date -Format 'HH:mm:ss')"

# --- 3. Install -------------------------------------------------------------
# npm, not pnpm: see the header note. --no-package-lock keeps the scratch
# directory from accruing a lockfile that would then be mistaken for the real
# one.
Set-Location $dst
Note "INSTALL_START $(Get-Date -Format 'HH:mm:ss')"

& 'F:\Node.s\npm.cmd' install --no-audit --no-fund --no-package-lock --loglevel=error 2>&1 |
    Out-File -FilePath (Join-Path $env:TEMP 'agent_npm.log') -Encoding utf8

Note "INSTALL_EXIT=$LASTEXITCODE"
Note "INSTALL_END $(Get-Date -Format 'HH:mm:ss')"
Note "HAS_TSC=$(Test-Path (Join-Path $dst 'node_modules\.bin\tsc.cmd'))"
Note "HAS_VITE=$(Test-Path (Join-Path $dst 'node_modules\.bin\vite.cmd'))"

# --- 4. Type check ---------------------------------------------------------
if (Test-Path (Join-Path $dst 'node_modules\.bin\tsc.cmd')) {
    Note "TYPECHECK_START $(Get-Date -Format 'HH:mm:ss')"

    & (Join-Path $dst 'node_modules\.bin\tsc.cmd') --noEmit 2>&1 |
        Out-File -FilePath (Join-Path $env:TEMP 'agent_typecheck.log') -Encoding utf8

    Note "TYPECHECK_EXIT=$LASTEXITCODE"
} else {
    Note 'TYPECHECK_EXIT=skipped (no tsc)'
}

Note "DONE $(Get-Date -Format 'HH:mm:ss')"
