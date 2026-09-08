$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimeDir = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$stopFile = Join-Path $runtimeDir 'stop'
if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
$pythonPath = (Get-Command python.exe).Source
$scriptPath = Join-Path $PSScriptRoot 'monitor.py'
$monitorProcess = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $scriptPath + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir 'stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'stderr.log')
Start-Sleep -Seconds 3
if ($monitorProcess.HasExited) {
    throw 'Monitor did not start. Check runtime/monitor.log; another instance may already be running.'
}
Write-Output ('Monitor started. PID: ' + $monitorProcess.Id)
Write-Output ('Status: ' + (Join-Path $runtimeDir 'status.json'))
