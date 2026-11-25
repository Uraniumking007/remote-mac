# Remote Mac prototype

View and control the Mac's existing desktop from Windows over Tailscale. The Mac must be awake, logged in, and unlocked. The prototype streams one selected display at a time and supports one Windows peer, keyboard shortcuts, clicks, dragging, scrolling, and explicit plain-text clipboard transfer.

## Mac host

Install Python 3.10 or newer and use the Tailscale IPv4 addresses for both devices. Run these commands from this directory:

```sh
xcrun swiftc -O -parse-as-library Capture.swift -o capture
xcrun clang -fobjc-arc -c VirtualDisplay.m -o /tmp/remote-mac-virtual.o
xcrun swiftc -O -import-objc-header VirtualDisplay.h Control.swift /tmp/remote-mac-virtual.o -o control
python3 -c 'import secrets; print(secrets.token_hex(32))'
python3 stream.py host MAC_TAILSCALE_IP WINDOWS_TAILSCALE_IP --control ./control
```

Keep the generated key private and paste it at both prompts. Enable Screen Recording for the capture process and Accessibility for the control helper when macOS prompts. Restart the host and reconnect after granting permissions if needed. Use a trusted tailnet and allow TCP ports 47332 (video) and 47333 (input) from your Windows PC. Both connections authenticate with the shared key and rely on Tailscale for encryption. Stop the host with Ctrl+C.

## Windows viewer

Copy `stream.py` and `desktop_viewer.py` to Windows. With Python 3.10 or newer, open PowerShell in that directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install av pygame-ce pyperclip keyring
.\.venv\Scripts\python.exe stream.py desktop MAC_TAILSCALE_IP
```

Windows stores the shared key in Credential Manager after authentication. Use `--replace-key` to enter a replacement.

Click the picture to take control. **F12** releases it. Focus loss also releases input, and a disconnected or unresponsive viewer causes the Mac helper to release its held keys and buttons. Close and reopen the viewer to reconnect.

- **Ctrl** sends Mac Control, including **Ctrl+C** in a terminal. **Left Alt** sends Mac Command, so **Left Alt+C** copies on the Mac. **Right Alt** sends Option. Use a US-layout keyboard on both sides for this initial physical key map.
- **F6** streams the built-in screen. **F7** adds a software 1920×1080 extended display. **F8** makes that virtual display primary. Click the picture to recapture input after switching. The virtual display is removed when the viewer disconnects.
- **F10** sends the Windows plain-text clipboard to the Mac. **F11** fetches the Mac clipboard to Windows. The viewer reserves F10–F12 for these local controls.
- The viewer scales the primary display to fit the window. It uses software H.264 decoding. The original ffplay-only baseline is still available with `stream.py view MAC_TAILSCALE_IP`.

## Compare with VNC

Use the same editor, terminal, resolution, and network for both tests. Type and select text, interrupt a running terminal command, drag and scroll, and move a multiline snippet both ways. Film the Windows keyboard and screen together in slow motion. Count frames between the keypress and visible change in the editor, then multiply by 1000 and divide by the recording frame rate. Repeat several times to capture both typical response and spikes. Check `tailscale ping` to see if the path was direct or relayed.

This build covers the unlocked desktop. Lid-closed operation needs a physical test. Login-screen and wake support, automatic reconnect, international keyboard layouts, and file clipboard formats are outside this prototype.
