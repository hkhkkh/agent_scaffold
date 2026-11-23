# Copy the frontend sources into the scratch dev directory.
#
# The Vite dev server runs from %TEMP%\agent_web (see build_frontend.ps1), so
# editing a file in the workspace does not reach it. Re-running the full build
# would reinstall every dependency just to move 25 source files, hence this.
# Vite's file watcher picks the copies up and hot-reloads on its own.

$ErrorActionPreference = 'Stop'

$src = 'h:\agent_scaffold\frontend\src'
$dst = Join-Path (Join-Path $env:TEMP 'agent_web') 'src'

if (-not (Test-Path $dst)) {
    Write-Error "The dev directory is missing: $dst. Run build_frontend.ps1 first."
}

# /MIR keeps deletions in step with the source, so a removed component does not
# linger and get imported by a stale path.
robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null

# robocopy exit codes below 8 mean success (1 = files copied).
if ($LASTEXITCODE -ge 8) {
    Write-Error "robocopy failed with exit code $LASTEXITCODE"
}

Write-Output "synced $(Get-Date -Format 'HH:mm:ss')"
