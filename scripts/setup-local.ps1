param([string]$PythonExe)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        $PythonExe = $bundled
    } else {
        $PythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}
& $PythonExe -X utf8 (Join-Path $PSScriptRoot 'setup-local.py')
if ($LASTEXITCODE -ne 0) { throw 'AgentSync local setup failed. See the error above.' }
