@echo off
setlocal
pushd "%~dp0" || exit /b 1
where dotnet >nul 2>&1
if errorlevel 1 (
    echo Install the .NET 11 SDK for Windows x64, then run this again.
    pause
    exit /b 1
)
dotnet publish RemoteMac.csproj -c Release -r win-x64 -o publish
if errorlevel 1 (
    echo.
    echo The build failed. See the error above.
    pause
    exit /b 1
)
if not exist "publish\RemoteMac.exe" (
    echo The expected EXE was not found.
    pause
    exit /b 1
)
if not exist "publish\RemoteMac.pri" if not exist "publish\resources.pri" (
    echo RemoteMac.exe was built, but its WinUI resources are missing.
    pause
    exit /b 1
)
explorer "%CD%\publish"
popd
