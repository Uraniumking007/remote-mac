using System.Net;
using System.Text;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Windows.ApplicationModel.DataTransfer;
using Windows.Foundation;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Security.Credentials;
using Windows.System;

namespace RemoteMac;

public sealed partial class MainWindow : Window
{
    private readonly SemaphoreSlim writes = new(1);
    private readonly DispatcherTimer heartbeat = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly MediaPlayer player = new();
    private readonly HashSet<int> keys = [];
    private readonly HashSet<int> buttons = [];
    private readonly Dictionary<VirtualKey, int> modifiers = new()
    {
        [VirtualKey.Control] = 59, [VirtualKey.Menu] = 55, [VirtualKey.Shift] = 56
    };
    private const string CredentialResource = "Remote Mac WinUI";
    private static readonly string MappingFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Remote Mac", "winui-mapping.json");
    private Connection? control;
    private VideoFeed? video;
    private CancellationTokenSource? session;
    private byte[]? pin, key;
    private string host = "";
    private string mode = "mirror";
    private bool captured, changing, fullscreen;
    private int width = 1, height = 1;
    private DateTime lastMove;
    private DateTime lastClick;
    private Point lastClickPoint;
    private int lastClickButton = -1;
    private int clickCount;

    public MainWindow()
    {
        InitializeComponent();
        Toolbar.Visibility = Visibility.Collapsed;
        try
        {
            var defaults = File.ReadAllLines(Path.Combine(AppContext.BaseDirectory, "ConnectionDefaults.txt"));
            if (defaults.Length == 2) { Address.Text = defaults[0]; Fingerprint.Text = defaults[1]; }
        }
        catch (IOException) { }
        Video.SetMediaPlayer(player);
        player.MediaFailed += (_, args) => DispatcherQueue.TryEnqueue(() => EndSession(args.ErrorMessage));
        heartbeat.Tick += async (_, _) => await ForwardAsync(new { kind = "heartbeat" }, false);
        Closed += (_, _) => { EndSession(); player.Dispose(); };
        Activated += async (_, args) =>
        {
            if (args.WindowActivationState == WindowActivationState.Deactivated) await ReleaseAsync();
        };
        try
        {
            var saved = System.Text.Json.JsonSerializer.Deserialize<Dictionary<int, int>>(File.ReadAllText(MappingFile));
            if (saved is not null)
                foreach (var modifier in modifiers.Keys.ToArray())
                    if (saved.TryGetValue((int)modifier, out int value) && value is 55 or 58 or 59 or 56)
                        modifiers[modifier] = value;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { }
    }

    private async void ConnectClicked(object sender, RoutedEventArgs args)
    {
        if (session is not null) return;
        host = Address.Text.Trim();
        if (!IPAddress.TryParse(host, out var address) || address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
        {
            Status.Text = "Enter the Mac’s IPv4 address.";
            return;
        }
        var lifetime = new CancellationTokenSource();
        session = lifetime;
        ConnectButton.IsEnabled = false;
        Status.Text = "Connecting…";
        try
        {
            pin = ParseHex(Fingerprint.Text.Replace(":", ""), "certificate fingerprint");
            var credentialName = host + "/" + Convert.ToHexString(pin);
            var vault = new PasswordVault();
            string text = PairingKey.Password.Trim();
            bool save = text.Length > 0;
            if (!save)
            {
                PasswordCredential saved;
                try { saved = vault.Retrieve(CredentialResource, credentialName); }
                catch (Exception error) when ((uint)error.HResult == 0x80070490)
                { throw new InvalidOperationException("Enter the pairing key shown by the Mac.", error); }
                saved.RetrievePassword();
                text = saved.Password;
            }
            key = ParseHex(text, "pairing key");
            var opened = await Connection.OpenAsync(host, 47333, pin, key, lifetime.Token);
            if (session != lifetime) { opened.Dispose(); return; }
            control = opened;
            using (var deadline = Deadline())
            {
                using var reply = await control.ReceiveAsync(deadline.Token);
                if (!reply.RootElement.GetProperty("ready").GetBoolean())
                    throw new IOException("The Mac control helper is unavailable.");
                await control.SendAsync(new { kind = "video-format", format = "framed-h264" }, deadline.Token);
                using var format = await control.ReceiveAsync(deadline.Token);
                if (format.RootElement.GetProperty("format").GetString() != "framed-h264")
                    throw new IOException("Update Remote Mac Host on the Mac.");
            }
            if (save) vault.Add(new PasswordCredential(CredentialResource, credentialName, text));
            PairingKey.Password = "";
            await ChangeModeAsync("mirror");
            if (session != lifetime) return;
            Setup.Visibility = Visibility.Collapsed;
            SessionSurface.Visibility = Visibility.Visible;
            Toolbar.Visibility = Visibility.Visible;
            heartbeat.Start();
        }
        catch (Exception error) { if (session == lifetime) EndSession(error.Message); }
        finally { ConnectButton.IsEnabled = true; }
    }

    private static byte[] ParseHex(string value, string label)
    {
        if (value.Trim().Length != 64) throw new FormatException($"Enter a valid {label}.");
        return Convert.FromHexString(value.Trim());
    }

    private CancellationTokenSource Deadline()
    {
        var timeout = CancellationTokenSource.CreateLinkedTokenSource(session!.Token);
        timeout.CancelAfter(TimeSpan.FromSeconds(5));
        return timeout;
    }

    private async Task ChangeModeAsync(string next)
    {
        if (changing || control is null || session is null) return;
        changing = true;
        var current = session;
        try
        {
            await ReleaseAsync();
            if (session != current) return;
            player.Source = null;
            video?.Dispose();
            video = null;
            using var deadline = Deadline();
            await writes.WaitAsync(deadline.Token);
            try
            {
                await control!.SendAsync(new { kind = "display-mode", mode = next }, deadline.Token);
                using var reply = await control.ReceiveAsync(deadline.Token);
                if (reply.RootElement.GetProperty("display").GetInt32() != 0) mode = next;
                Title = $"Remote Mac · {mode}";
            }
            finally { writes.Release(); }
            var incoming = await Connection.OpenAsync(host, 47332, pin!, key!, deadline.Token);
            try { video = await VideoFeed.StartAsync(incoming, deadline.Token); }
            catch { incoming.Dispose(); throw; }
            width = (int)video.Width;
            height = (int)video.Height;
            player.Source = MediaSource.CreateFromMediaStreamSource(video.Source);
            player.Play();
        }
        catch (Exception error) { if (session == current) EndSession(error.Message); }
        finally { changing = false; }
    }

    private async Task ForwardAsync(object message, bool dropIfBusy = false)
    {
        if (control is not { } connection || session is not { } current) return;
        var acquired = false;
        try
        {
            using var deadline = Deadline();
            acquired = await writes.WaitAsync(dropIfBusy ? 0 : 1000, deadline.Token);
            if (acquired && session == current) await connection.SendAsync(message, deadline.Token);
            else if (!acquired && !dropIfBusy) EndSession("The Mac stopped responding to input.");
        }
        catch (Exception error) { if (session == current) EndSession(error.Message); }
        finally { if (acquired) writes.Release(); }
    }

    private async Task ReleaseAsync()
    {
        if (!captured) return;
        captured = false;
        keys.Clear();
        buttons.Clear();
        InputSurface.ReleasePointerCaptures();
        await ForwardAsync(new { kind = "release" });
    }

    private void EndSession(string reason = "")
    {
        heartbeat.Stop();
        captured = false;
        keys.Clear();
        buttons.Clear();
        session?.Cancel();
        session = null;
        player.Source = null;
        video?.Dispose();
        video = null;
        control?.Dispose();
        control = null;
        if (fullscreen) ToggleFullscreen();
        Setup.Visibility = Visibility.Visible;
        SessionSurface.Visibility = Visibility.Collapsed;
        Toolbar.Visibility = Visibility.Collapsed;
        Status.Text = reason;
        Title = "Remote Mac";
    }

    private void ToggleFullscreen()
    {
        fullscreen = !fullscreen;
        AppWindow.SetPresenter(fullscreen ? AppWindowPresenterKind.FullScreen : AppWindowPresenterKind.Default);
        Toolbar.Visibility = fullscreen ? Visibility.Collapsed : Visibility.Visible;
        ExitFullscreen.Visibility = fullscreen ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void MirrorClicked(object sender, RoutedEventArgs args) => await ChangeModeAsync("mirror");
    private async void ExtendClicked(object sender, RoutedEventArgs args) => await ChangeModeAsync("extend");
    private async void PrimaryClicked(object sender, RoutedEventArgs args) => await ChangeModeAsync("primary");
    private async void ReleaseClicked(object sender, RoutedEventArgs args) => await ReleaseAsync();
    private async void InputLostFocus(object sender, RoutedEventArgs args) => await ReleaseAsync();
    private async void InputCaptureLost(object sender, PointerRoutedEventArgs args)
    {
        if (buttons.Count > 0) await ReleaseAsync();
    }
    private void DisconnectClicked(object sender, RoutedEventArgs args) => EndSession();
    private void FullscreenClicked(object sender, RoutedEventArgs args)
    {
        ToggleFullscreen();
        InputSurface.Focus(FocusState.Programmatic);
    }

    private void FullscreenShortcut(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs args)
    {
        args.Handled = true;
        if (session is not null && SessionSurface.Visibility == Visibility.Visible)
            FullscreenClicked(sender, new RoutedEventArgs());
    }

    private async void InputKeyDown(object sender, KeyRoutedEventArgs args)
    {
        if (args.Key == VirtualKey.F12) { await ReleaseAsync(); args.Handled = true; return; }
        string? requested = args.Key switch { VirtualKey.F6 => "mirror", VirtualKey.F7 => "extend",
            VirtualKey.F8 => "primary", _ => null };
        if (requested is not null)
        {
            args.Handled = true;
            if (!args.KeyStatus.WasKeyDown) await ChangeModeAsync(requested);
            return;
        }
        if (!captured || !MacKey(args.Key, out int code)) return;
        keys.Add(code);
        args.Handled = true;
        await ForwardAsync(new { kind = "key", code, down = true });
    }

    private async void InputKeyUp(object sender, KeyRoutedEventArgs args)
    {
        if (captured && MacKey(args.Key, out int code) && keys.Remove(code))
        {
            args.Handled = true;
            await ForwardAsync(new { kind = "key", code, down = false });
        }
    }

    private bool MacKey(VirtualKey key, out int code)
    {
        if (modifiers.TryGetValue(key, out code)) return true;
        return KeyCodes.TryGetValue(key, out code);
    }

    private static readonly Dictionary<VirtualKey, int> KeyCodes = new()
    {
        [VirtualKey.Left]=123, [VirtualKey.Right]=124, [VirtualKey.Down]=125, [VirtualKey.Up]=126,
        [VirtualKey.Enter]=36, [VirtualKey.Tab]=48, [VirtualKey.Space]=49, [VirtualKey.Back]=51,
        [VirtualKey.Escape]=53, [VirtualKey.Delete]=117, [VirtualKey.Home]=115, [VirtualKey.End]=119,
        [VirtualKey.PageUp]=116, [VirtualKey.PageDown]=121, [(VirtualKey)0x14]=57,
    };

    static MainWindow()
    {
        int[] letters = [0,11,8,2,14,3,5,4,34,38,40,37,46,45,31,35,12,15,1,17,32,9,13,7,16,6];
        int[] digits = [29,18,19,20,21,23,22,26,28,25];
        for (int i = 0; i < letters.Length; i++) KeyCodes[(VirtualKey)(65+i)] = letters[i];
        for (int i = 0; i < digits.Length; i++) KeyCodes[(VirtualKey)(48+i)] = digits[i];
        foreach (var (windows, mac) in new (int,int)[] { (186,41),(187,24),(188,43),(189,27),
            (190,47),(191,44),(192,50),(219,33),(220,42),(221,30),(222,39) })
            KeyCodes[(VirtualKey)windows] = mac;
    }

    private (double x, double y, bool inside) MapPoint(Point pos)
    {
        double scale = Math.Min(InputSurface.ActualWidth / width, InputSurface.ActualHeight / height);
        if (scale <= 0) return (0, 0, false);
        double w = width * scale, h = height * scale;
        double x = (pos.X - (InputSurface.ActualWidth - w) / 2) / w;
        double y = (pos.Y - (InputSurface.ActualHeight - h) / 2) / h;
        return (Math.Clamp(x, 0, 1), Math.Clamp(y, 0, 1), x is >= 0 and <= 1 && y is >= 0 and <= 1);
    }

    private async void InputPressed(object sender, PointerRoutedEventArgs args)
    {
        var point = args.GetCurrentPoint(InputSurface);
        var (x, y, inside) = MapPoint(point.Position);
        if (!inside) return;
        args.Handled = true;
        InputSurface.Focus(FocusState.Pointer);
        if (!captured) { captured = true; return; }
        int button = point.Properties.IsRightButtonPressed ? 1 : point.Properties.IsMiddleButtonPressed ? 2 : 0;
        buttons.Add(button);
        InputSurface.CapturePointer(args.Pointer);
        var now = DateTime.UtcNow;
        bool close = Math.Abs(point.Position.X - lastClickPoint.X) < 5 && Math.Abs(point.Position.Y - lastClickPoint.Y) < 5;
        clickCount = button == lastClickButton && close && (now - lastClick).TotalMilliseconds < 400
            ? Math.Min(3, clickCount + 1) : 1;
        lastClick = now;
        lastClickPoint = point.Position;
        lastClickButton = button;
        await ForwardAsync(new { kind = "button", button, x, y, down = true, clicks = clickCount });
    }

    private async void InputReleased(object sender, PointerRoutedEventArgs args)
    {
        if (!captured || buttons.Count == 0) return;
        var point = args.GetCurrentPoint(InputSurface);
        var (x, y, _) = MapPoint(point.Position);
        bool[] pressed = [point.Properties.IsLeftButtonPressed, point.Properties.IsRightButtonPressed, point.Properties.IsMiddleButtonPressed];
        foreach (int button in buttons.Where(button => !pressed[button]).ToArray())
        {
            buttons.Remove(button);
            await ForwardAsync(new { kind = "button", button, x, y, down = false, clicks = button == lastClickButton ? clickCount : 1 });
        }
    }

    private async void InputMoved(object sender, PointerRoutedEventArgs args)
    {
        var now = DateTime.UtcNow;
        if (!captured || (now - lastMove).TotalMilliseconds < 8) return;
        var (x, y, inside) = MapPoint(args.GetCurrentPoint(InputSurface).Position);
        if (!inside && buttons.Count == 0) return;
        lastMove = now;
        await ForwardAsync(new { kind = "move", x, y }, true);
    }

    private async void InputScrolled(object sender, PointerRoutedEventArgs args)
    {
        if (!captured) return;
        var props = args.GetCurrentPoint(InputSurface).Properties;
        int lines = Math.Clamp(props.MouseWheelDelta / 120, -120, 120);
        await ForwardAsync(new { kind = "scroll", x = props.IsHorizontalMouseWheel ? lines : 0,
            y = props.IsHorizontalMouseWheel ? 0 : lines });
    }

    private async void SendClipboardClicked(object sender, RoutedEventArgs args)
    {
        var content = Clipboard.GetContent();
        if (!content.Contains(StandardDataFormats.Text)) return;
        string text = await content.GetTextAsync();
        if (Encoding.UTF8.GetByteCount(text) <= 128_000)
            await ForwardAsync(new { kind = "clipboard-set", text });
    }

    private async void GetClipboardClicked(object sender, RoutedEventArgs args)
    {
        if (control is not { } connection || session is not { } current) return;
        try
        {
            using var deadline = Deadline();
            await writes.WaitAsync(deadline.Token);
            try
            {
                await connection.SendAsync(new { kind = "clipboard-get" }, deadline.Token);
                using var reply = await connection.ReceiveAsync(deadline.Token);
                var data = new DataPackage();
                data.SetText(reply.RootElement.GetProperty("text").GetString() ?? "");
                Clipboard.SetContent(data);
            }
            finally { writes.Release(); }
        }
        catch (Exception error) { if (session == current) EndSession(error.Message); }
    }

    private async void MappingClicked(object sender, RoutedEventArgs args)
    {
        await ReleaseAsync();
        var rows = new StackPanel { Spacing = 12 };
        int[] options = [55, 58, 59, 56];
        VirtualKey[] choices = [VirtualKey.Menu, VirtualKey.Control, VirtualKey.Shift];
        var boxes = new List<ComboBox>();
        foreach (var source in choices)
        {
            var combo = new ComboBox { Header = source == VirtualKey.Menu ? "Alt" : source.ToString(),
                ItemsSource = new[] { "Command", "Option", "Control", "Shift" },
                SelectedIndex = Array.IndexOf(options, modifiers[source]), MinWidth = 200 };
            boxes.Add(combo);
            rows.Children.Add(combo);
        }
        var dialog = new ContentDialog { XamlRoot = Root.XamlRoot, Title = "Key mapping", Content = rows,
            PrimaryButtonText = "Save", CloseButtonText = "Cancel", DefaultButton = ContentDialogButton.Primary };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary) return;
        for (int i = 0; i < choices.Length; i++) modifiers[choices[i]] = options[boxes[i].SelectedIndex];
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(MappingFile)!);
            File.WriteAllText(MappingFile, System.Text.Json.JsonSerializer.Serialize(
                modifiers.ToDictionary(entry => (int)entry.Key, entry => entry.Value)));
        }
        catch (IOException) { Title = "Remote Mac · Key mapping saved for this session only"; }
    }
}
