@echo off
setlocal
cd /d "%~dp0"
set "REMOTEMAC_EXE=%CD%\publish\RemoteMac.exe"
if exist "%CD%\RemoteMac.exe" set "REMOTEMAC_EXE=%CD%\RemoteMac.exe"
if not exist "%REMOTEMAC_EXE%" (
    echo Place this script next to RemoteMac.exe or in the folder containing publish.
    pause
    exit /b 1
)
set "REMOTEMAC_LOG=%TEMP%\RemoteMac-startup.txt"
powershell.exe -NoProfile -Command "& { $started = Get-Date; $exe = $env:REMOTEMAC_EXE; $folder = Split-Path $exe; foreach ($file in @('RemoteMac.exe','RemoteMac.dll','RemoteMac.runtimeconfig.json','hostfxr.dll','Microsoft.UI.Xaml.dll','RemoteMac.pri','resources.pri')) { '{0}: {1}' -f $file,(Test-Path (Join-Path $folder $file)) }; try { $app = Start-Process -FilePath $exe -WorkingDirectory $folder -PassThru -ErrorAction Stop; if ($app.WaitForExit(6000)) { 'Exit code: {0} (0x{0:X8})' -f $app.ExitCode } else { 'Process stayed open for at least six seconds. Main window handle: {0}' -f $app.MainWindowHandle } } catch { 'Launch failed: ' + $_.Exception.Message }; Start-Sleep -Seconds 2; Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = $started.AddSeconds(-1); Id = 1000,1001,1026 } -ErrorAction SilentlyContinue | Where-Object { $_.Message -match '(?i)RemoteMac\.(exe|dll)' } | Select-Object -First 6 | Format-List TimeCreated,ProviderName,Id,Message } | Out-File -Encoding utf8 $env:REMOTEMAC_LOG"
notepad "%REMOTEMAC_LOG%"
