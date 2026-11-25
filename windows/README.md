# Remote Mac for Windows

This preview needs Windows 11 x64 and the paired-device Mac host. The
older Python viewer is a latency baseline and cannot negotiate framed video.

## Build

On Windows, install the .NET 11 SDK and Windows App SDK build prerequisites.
Then run PowerShell from this folder.

```powershell
dotnet publish .\RemoteMac.csproj -c Release -r win-x64 -o .\publish
```

Open `publish\RemoteMac.exe` from the publish folder. Keep its DLLs beside it.

## Connect

Open the new Remote Mac Host on the Mac and select **Pair Windows PC**.
Enter the PC's current LAN IPv4 address and approve it. Enter the resulting
key in the Windows app once. The app stores it in Windows Credential Manager.
If you approve a new key, enter it in the Windows app to replace its saved key.
The host supports one approved PC at a time. **Revoke Windows PC** disconnects
the viewer, removes the device key, and stops accepting connections.

Click the remote desktop to capture input. F6 mirrors, F7 extends, F8 makes
the virtual display primary, F9 toggles fullscreen, and F12 releases input.
The toolbar also exposes modes, clipboard, and modifier mapping.

## Performance verification

Confirm that the session displays video and handles input before benchmarking.
Use Task Manager to check the GPU Video Decode engine and the app's CPU usage.
Record keyboard-to-visible-response latency at 1920×1080 using the same editor,
LAN and display mode for both this viewer and the Python viewer. Windows'
media pipeline chooses the decoder. Hardware use and latency depend on the tested
GPU, driver, and media pipeline.
