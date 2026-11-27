"""Focused SDL viewer with software H.264 decode for the control prototype."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

from remote_mac_protocol import CHUNK_SIZE, CONTROL_PORT, PORT, authenticate_viewer, receive_message, send_message

# SDL scancodes follow USB usage IDs. Values are macOS virtual key codes.
MAC_KEYS = dict(zip(range(4, 30), (
    0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35,
    12, 15, 1, 17, 32, 9, 13, 7, 16, 6,
)))
MAC_KEYS.update(dict(zip(range(30, 40), (18, 19, 20, 21, 23, 22, 26, 28, 25, 29))))
MAC_KEYS.update({
    40: 36, 41: 53, 42: 51, 43: 48, 44: 49, 45: 27, 46: 24,
    47: 33, 48: 30, 49: 42, 51: 41, 52: 39, 53: 50, 54: 43,
    55: 47, 56: 44, 57: 57, 58: 122, 59: 120, 60: 99, 61: 118,
    62: 96, 63: 97, 64: 98, 65: 100, 66: 101, 67: 109, 68: 103,
    69: 111, 73: 114, 74: 115, 75: 116, 76: 117, 77: 119, 78: 121,
    79: 124, 80: 123, 81: 125, 82: 126, 88: 76,
    224: 59, 225: 56, 226: 55, 228: 62, 229: 60, 230: 58,
})
MAC_BUTTONS = {1: 0, 3: 1, 2: 2}


def connect(host, port, key, fingerprint=None):
    connection = socket.create_connection((host, port), timeout=5)
    try:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if fingerprint is not None:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.minimum_version = ssl.TLSVersion.TLSv1_3
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
            connection = tls.wrap_socket(connection, server_hostname=host)
            actual = hashlib.sha256(connection.getpeercert(binary_form=True)).digest()
            if not hmac.compare_digest(actual, fingerprint):
                raise ConnectionError("Mac certificate fingerprint does not match")
        authenticate_viewer(connection, key)
        return connection
    except BaseException:
        connection.close()
        raise


def decode(connection, av, state):
    try:
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}
        while chunk := connection.recv(CHUNK_SIZE):
            for packet in codec.parse(chunk):
                for frame in codec.decode(packet):
                    rgb = frame.reformat(format="rgb24")
                    plane = rgb.planes[0]
                    data = bytes(plane)
                    row = rgb.width * 3
                    if plane.line_size != row:
                        data = b"".join(
                            data[offset:offset + row]
                            for offset in range(0, plane.line_size * rgb.height, plane.line_size)
                        )
                    state["frame"] = (rgb.width, rgb.height, data)
        state["error"] = "Mac ended the video stream"
    except (ImportError, OSError, ValueError) as error:
        state["error"] = f"Video disconnected: {error}"


def run(host, key, fingerprint=None, on_authenticated=None):
    try:
        import av
        import pygame
        import pyperclip
    except ImportError as error:
        raise ConnectionError("Install av, pygame-ce, and pyperclip. See README.md.") from error
    with connect(host, CONTROL_PORT, key, fingerprint) as control:
        if on_authenticated is not None:
            on_authenticated()
        if json.loads(receive_message(control)) != {"ready": True}:
            raise ConnectionError("Mac control helper unavailable")
        mode = "mirror"
        requested = mode
        resize = None
        try:
            while requested is not None:
                control.settimeout(5)
                send_message(control, {"kind": "display-mode", "mode": requested})
                reply = json.loads(receive_message(control))
                notice = ""
                if reply.get("display"):
                    mode = requested
                else:
                    notice = f" · {requested.title()} unavailable"
                control.settimeout(1)
                with connect(host, PORT, key, fingerprint) as video:
                    video.settimeout(None)
                    state = {}
                    decoder = threading.Thread(target=decode, args=(video, av, state), daemon=True)
                    decoder.start()
                    try:
                        requested, resize = window_loop(pygame, pyperclip, control, state, mode, notice, resize)
                    finally:
                        try:
                            video.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        decoder.join(timeout=1)
        finally:
            pygame.quit()


DISPLAY_KEYS = {63: "mirror", 64: "extend", 65: "primary"}
FULLSCREEN_SCANCODE = 66
CLIPBOARD_SCANCODES = {67: "send", 68: "fetch"}
RELEASE_SCANCODE = 69
PAUSE_SCANCODE = 72
LOCAL_SCANCODES = frozenset({*DISPLAY_KEYS, FULLSCREEN_SCANCODE, *CLIPBOARD_SCANCODES, RELEASE_SCANCODE, PAUSE_SCANCODE})
MAC_FN = 63
FN_FALLBACKS = (71, 101, None)
FN_FALLBACK_LABELS = {71: "Scroll Lock", 101: "Menu", None: "Off"}
DEFAULT_FN_FALLBACK = 71
FN_LABELS = frozenset({"fn", "function", "globe"})
BAR_GROUPS = (
    ("Display", ("mirror", "extend", "primary")),
    ("Keyboard", ("release", "keymap")),
    ("Clipboard", ("send", "fetch")),
    ("View", ("fullscreen",)),
    ("Session", ("disconnect",)),
)
BAR_ACTIONS = tuple(action for _, actions in BAR_GROUPS for action in actions)
BUTTON_LABELS = {
    "mirror": "Mirror  F6",
    "extend": "Extend  F7",
    "primary": "Primary  F8",
    "release": "Release input  F12",
    "keymap": "Key mapping",
    "send": "Send clipboard  F10",
    "fetch": "Get clipboard  F11",
    "disconnect": "Disconnect",
}


REMAPPABLE_KEYS = {226: "Left Alt", 224: "Left Ctrl", 225: "Left Shift",
                   230: "Right Alt", 228: "Right Ctrl", 229: "Right Shift"}
MAC_MODIFIERS = {55: "Command", 58: "Option", 59: "Control", 56: "Shift", None: "Disabled"}
MODIFIER_DEFAULTS = {key: {62: 59, 60: 56}.get(MAC_KEYS[key], MAC_KEYS[key]) for key in REMAPPABLE_KEYS}


def default_mapping():
    mapping = MODIFIER_DEFAULTS.copy()
    mapping["fn"] = DEFAULT_FN_FALLBACK
    return mapping


def button_label(action, fullscreen):
    if action == "fullscreen":
        return "Exit fullscreen" if fullscreen else "Fullscreen  F9"
    return BUTTON_LABELS[action]


def layout_connection_bar(groups, button_width, window_width):
    pad_x = 12
    button_gap = 6
    group_gap = 18
    button_h = 27
    x = pad_x
    y = 52
    row_bottom = y + button_h
    rects = {}
    labels = []
    for title, actions in groups:
        widths = [button_width(action) for action in actions]
        group_w = sum(widths) + button_gap * max(0, len(actions) - 1)
        if x > pad_x and x + group_w > window_width - pad_x:
            x = pad_x
            y = row_bottom + 24
            row_bottom = y + button_h
        labels.append((title, x, y - 16))
        for action, width in zip(actions, widths):
            rects[action] = (x, y, width, button_h)
            x += width + button_gap
        x += group_gap - button_gap
    return rects, row_bottom + 10, labels


def connection_bar_open(fullscreen, mouse_y, bar_height, now, hold_until, was_open):
    if not fullscreen:
        return True
    if now < hold_until:
        return True
    if mouse_y <= 3:
        return True
    return bool(was_open and mouse_y < bar_height)


def pointer_in_bar(bar_open, bar_height, y):
    return bool(bar_open and y < bar_height)


def move_bar_focus(current, count, backward):
    if count <= 0:
        return None
    if current is None:
        return count - 1 if backward else 0
    step = -1 if backward else 1
    return (current + step) % count


def connection_status(captured, feedback, fullscreen, has_frame=True):
    if feedback:
        return feedback
    if not has_frame and not captured:
        return "Waiting for the Mac picture."
    if captured and fullscreen:
        return "Keyboard is on the Mac. Ctrl+Alt+Pause or F9 exits fullscreen. F12 releases input."
    if captured:
        return "Keyboard is on the Mac. F12 releases input."
    if fullscreen:
        return "Pointer at the top shows controls. Ctrl+Alt+Pause or F9 exits fullscreen."
    return "Keyboard is on this PC. Click the picture to control the Mac."


def feedback_color(feedback, captured):
    if feedback:
        lowered = feedback.lower()
        if lowered.startswith("could not") or "exceeds" in lowered or "unavailable" in lowered:
            return (255, 176, 156)
        return (128, 225, 184)
    if captured:
        return (128, 225, 184)
    return (186, 204, 224)


def ctrl_alt_pause(scancode, ctrl_down, alt_down):
    return scancode == PAUSE_SCANCODE and ctrl_down and alt_down


def fn_label(text):
    label = str(text or "").strip().lower()
    return label in FN_LABELS


def is_physical_fn(key_label, scancode, reserved=LOCAL_SCANCODES):
    if scancode in reserved:
        return False
    return fn_label(key_label)


def forwarded_mac_code(scancode, mapping, physical_fn):
    if scancode in LOCAL_SCANCODES:
        return None
    if physical_fn or scancode == mapping.get("fn"):
        return MAC_FN
    if scancode in REMAPPABLE_KEYS:
        return mapping.get(scancode)
    return MAC_KEYS.get(scancode)


# SDL scancodes for the six remappable modifiers, mapped to Win32 virtual keys.
WINDOWS_MODIFIERS = {224: 0xA2, 225: 0xA0, 226: 0xA4,
                     228: 0xA3, 229: 0xA1, 230: 0xA5}


def windows_key_state():
    if sys.platform != "win32":
        return None
    import ctypes

    get_state = ctypes.WinDLL("user32").GetAsyncKeyState
    get_state.argtypes = (ctypes.c_int,)
    get_state.restype = ctypes.c_short
    return get_state


def load_key_mapping(path):
    mapping = default_mapping()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return mapping
    if isinstance(saved, dict):
        for key in REMAPPABLE_KEYS:
            value = saved.get(str(key), mapping[key])
            if (value is None or type(value) is int) and value in MAC_MODIFIERS:
                mapping[key] = value
        if "fn" in saved and saved["fn"] in FN_FALLBACKS:
            mapping["fn"] = saved["fn"]
    return mapping


def save_key_mapping(path, mapping):
    payload = {str(key): mapping[key] for key in REMAPPABLE_KEYS}
    payload["fn"] = mapping.get("fn", DEFAULT_FN_FALLBACK)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(payload), encoding="utf-8")
    pending.replace(path)


def window_loop(pg, clipboard, control, state, mode, notice, resize):
    pg.init()
    window = pg.display.get_surface()
    if window is None:
        window = pg.display.set_mode(resize or (960, 600), pg.RESIZABLE)
    fullscreen = bool(window.get_flags() & pg.FULLSCREEN)
    windowed_size = resize or ((960, 600) if fullscreen else window.get_size())
    fullscreen_key_held = False
    pause_combo_held = False
    pg.display.set_caption("Remote Mac")
    heading = pg.font.SysFont("Segoe UI", 19, bold=True)
    font = pg.font.SysFont("Segoe UI", 15)
    small = pg.font.SysFont("Segoe UI", 12)
    window.fill("black")
    pg.display.flip()
    pg.key.set_repeat(420, 40)
    clock = pg.time.Clock()
    frame = None
    surface = None
    area = None
    drawn_size = None
    toolbar = {}
    toolbar_height = 0
    group_labels = []
    scaled = None
    last_paint = None
    feedback = notice.strip(" ·")
    captured = False
    bar_open = not fullscreen
    bar_hold_until = time.monotonic() + 3 if fullscreen else 0
    bar_focus = None
    mapping_path = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "Remote Mac" / "key-mapping.json"
    mapping = load_key_mapping(mapping_path)
    settings_open = False
    settings_focus = 0
    settings_items = {}
    settings_keys = (*REMAPPABLE_KEYS, "fn", "reset", "done")
    held_keys = {}
    physical_key_state = windows_key_state()
    held_buttons = set()
    clicks = {}
    click_counts = {}
    heartbeat = 0
    caption_cache = {}

    def send(kind, **fields):
        send_message(control, {"kind": kind, **fields})

    def point(position):
        return {
            "x": min(1, max(0, (position[0] - area.x) / max(1, area.width - 1))),
            "y": min(1, max(0, (position[1] - area.y) / max(1, area.height - 1))),
        }

    def release_key(scancode):
        code = held_keys.pop(scancode, None)
        if code is not None and code not in held_keys.values():
            send("key", code=code, down=False)

    def release_input():
        nonlocal captured
        if captured:
            send("release")
            captured = False
            held_buttons.clear()
            held_keys.clear()
            pg.event.set_grab(False)

    def transfer(action):
        nonlocal feedback
        if action == "send":
            value = clipboard.paste()
            if len(value.encode("utf-8")) > 128_000:
                feedback = "Clipboard text exceeds 128 KB"
                return
            send("clipboard-set", text=value)
            feedback = "Clipboard sent to Mac"
        else:
            send("clipboard-get")
            clipboard.copy(json.loads(receive_message(control))["text"])
            feedback = "Clipboard copied from Mac"

    def edit_mapping(action):
        nonlocal settings_open, feedback
        if action == "done":
            settings_open = False
            return
        updated = mapping.copy()
        if action == "reset":
            updated = default_mapping()
        elif action == "fn":
            choices = FN_FALLBACKS
            updated["fn"] = choices[(choices.index(mapping["fn"]) + 1) % len(choices)]
        else:
            choices = tuple(MAC_MODIFIERS)
            updated[action] = choices[(choices.index(mapping[action]) + 1) % len(choices)]
        try:
            save_key_mapping(mapping_path, updated)
        except OSError:
            feedback = "Could not save key mapping"
        else:
            mapping.update(updated)
            feedback = "Key mapping saved"

    def session_size():
        return windowed_size if fullscreen else window.get_size()

    def caption(action):
        label = button_label(action, fullscreen)
        cached = caption_cache.get(label)
        if cached is None:
            cached = font.render(label, True, (235, 241, 249))
            caption_cache[label] = cached
        return cached

    def sdl_key_label(key):
        try:
            return pg.key.name(key, use_compat=False)
        except TypeError:
            return pg.key.name(key)
        except Exception:
            return ""

    def modifiers_down(event):
        ctrl = bool(event.mod & pg.KMOD_CTRL)
        alt = bool(event.mod & pg.KMOD_ALT)
        if physical_key_state is not None:
            ctrl = ctrl or bool(physical_key_state(0x11) & 0x8000)
            alt = alt or bool(physical_key_state(0x12) & 0x8000)
        return ctrl, alt

    def toggle_fullscreen():
        nonlocal fullscreen, window, windowed_size, drawn_size, feedback, bar_hold_until, bar_open
        release_input()
        if not fullscreen:
            windowed_size = window.get_size()
        try:
            window = pg.display.set_mode(windowed_size, pg.RESIZABLE) if fullscreen else pg.display.set_mode((0, 0), pg.FULLSCREEN)
        except pg.error:
            feedback = "Could not switch fullscreen"
        else:
            fullscreen = not fullscreen
            drawn_size = None
            feedback = ""
            if fullscreen:
                bar_hold_until = time.monotonic() + 3
                bar_open = True

    while True:
        if state.get("error"):
            raise ConnectionError(state["error"])
        current = state.get("frame")
        window_size = window.get_size()
        mouse_pos = pg.mouse.get_pos()
        now = time.monotonic()
        bar_open = connection_bar_open(fullscreen, mouse_pos[1], toolbar_height or 89, now, bar_hold_until, bar_open)
        resized = window_size != drawn_size
        new_frame = current is not frame
        if resized:
            def button_width(action):
                return caption(action).get_width() + 22

            rects, toolbar_height, group_labels = layout_connection_bar(BAR_GROUPS, button_width, window_size[0])
            toolbar = {action: pg.Rect(rect) for action, rect in rects.items()}
            drawn_size = window_size
            bar_open = connection_bar_open(fullscreen, mouse_pos[1], toolbar_height, now, bar_hold_until, bar_open)
        if current is not None and (new_frame or resized):
            if new_frame:
                frame = current
                surface = pg.image.frombuffer(frame[2], frame[:2], "RGB")
            top = 0 if fullscreen else toolbar_height
            viewport = pg.Rect(0, top, window_size[0], max(1, window_size[1] - top))
            area = surface.get_rect().fit(viewport)
            area.center = viewport.center
            scaled = None
            if area.width and area.height:
                scaled = surface if surface.get_size() == area.size else pg.transform.smoothscale(surface, area.size)

        visible = toolbar if bar_open else {}
        hovered = next((action for action, rect in visible.items() if rect.collidepoint(mouse_pos)), None)
        status_text = connection_status(captured, feedback, fullscreen, frame is not None)
        paint = (captured, feedback, hovered, settings_open, settings_focus, bar_focus, bar_open, fullscreen,
                 tuple(mapping.values()), status_text)
        if resized or new_frame or paint != last_paint:
            window.fill((6, 10, 16))
            if scaled is not None:
                window.blit(scaled, area)
            if bar_open:
                pg.draw.rect(window, (19, 27, 41), (0, 0, window_size[0], toolbar_height))
                pg.draw.line(window, (53, 67, 88), (0, toolbar_height - 1), (window_size[0], toolbar_height - 1))
                title_surface = heading.render("Remote Mac", True, (239, 245, 253))
                window.blit(title_surface, (12, 8))
                color = feedback_color(feedback, captured)
                window.blit(font.render(status_text, True, color), (title_surface.get_width() + 20, 11))
                for title, x, y in group_labels:
                    window.blit(small.render(title, True, (148, 168, 194)), (x, y))
                focused = None if captured or bar_focus is None else BAR_ACTIONS[bar_focus]
                for action, rect in visible.items():
                    if action == "fullscreen" and fullscreen:
                        color = (184, 112, 64) if action == hovered else (158, 92, 48)
                    elif action == mode:
                        color = (41, 92, 152)
                    elif action == hovered:
                        color = (57, 73, 99)
                    else:
                        color = (34, 46, 65)
                    pg.draw.rect(window, color, rect, border_radius=5)
                    if action == focused:
                        pg.draw.rect(window, (135, 187, 246), rect, 2, border_radius=5)
                    label = caption(action)
                    window.blit(label, label.get_rect(center=rect.center))
            if settings_open:
                settings_items.clear()
                row_keys = (*REMAPPABLE_KEYS, "fn")
                width = max(280, min(540, window_size[0] - 16))
                panel = pg.Rect(0, 0, width, min(window_size[1] - 8, 124 + len(row_keys) * 30))
                panel.center = (window_size[0] // 2, window_size[1] // 2)
                panel.top = max(4, panel.top)
                pg.draw.rect(window, (24, 34, 51), panel, border_radius=8)
                pg.draw.rect(window, (84, 111, 147), panel, 1, border_radius=8)
                window.blit(heading.render("Key mapping", True, (239, 245, 253)), (panel.x + 14, panel.y + 10))
                window.blit(small.render("Scroll Lock is the Fn fallback. A detected Fn key is sent too.", True,
                                         (164, 183, 207)), (panel.x + 14, panel.y + 36))
                window.blit(small.render("Globe gestures and firmware-only keys are not forwarded.", True,
                                         (164, 183, 207)), (panel.x + 14, panel.y + 54))
                for index, key in enumerate(row_keys):
                    y = panel.y + 78 + index * 30
                    if key == "fn":
                        title = "Fn / Globe fallback"
                        label = FN_FALLBACK_LABELS[mapping["fn"]]
                    else:
                        title = REMAPPABLE_KEYS[key]
                        label = MAC_MODIFIERS[mapping[key]]
                    window.blit(font.render(title, True, (235, 241, 249)), (panel.x + 14, y + 5))
                    rect = pg.Rect(panel.right - 150, y, 136, 25)
                    settings_items[key] = rect
                    selected = settings_focus == index
                    pg.draw.rect(window, (49, 77, 110) if selected else (34, 46, 65), rect, border_radius=4)
                    if selected:
                        pg.draw.rect(window, (135, 187, 246), rect, 1, border_radius=4)
                    text = font.render(label, True, (235, 241, 249))
                    window.blit(text, text.get_rect(center=rect.center))
                for key, place in (("reset", 0), ("done", 1)):
                    rect = pg.Rect(panel.x + 14 if key == "reset" else panel.right - 84, panel.bottom - 39,
                                   130 if key == "reset" else 70, 27)
                    settings_items[key] = rect
                    selected = settings_focus == settings_keys.index(key)
                    pg.draw.rect(window, (49, 77, 110) if selected else (34, 46, 65), rect, border_radius=4)
                    if selected:
                        pg.draw.rect(window, (135, 187, 246), rect, 1, border_radius=4)
                    text = font.render("Reset defaults" if key == "reset" else "Done", True, (235, 241, 249))
                    window.blit(text, text.get_rect(center=rect.center))
            last_paint = paint
            pg.display.flip()

        for event in pg.event.get():
            if event.type == pg.QUIT:
                return None, session_size()
            if settings_open:
                if event.type == pg.KEYUP and event.scancode == FULLSCREEN_SCANCODE:
                    fullscreen_key_held = False
                elif event.type == pg.KEYUP and event.scancode == PAUSE_SCANCODE:
                    pause_combo_held = False
                elif event.type == pg.KEYDOWN:
                    if event.key == pg.K_ESCAPE:
                        settings_open = False
                    elif event.key == pg.K_TAB:
                        direction = -1 if event.mod & pg.KMOD_SHIFT else 1
                        settings_focus = (settings_focus + direction) % len(settings_keys)
                    elif event.key in (pg.K_RETURN, pg.K_SPACE):
                        edit_mapping(settings_keys[settings_focus])
                elif event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
                    for index, key in enumerate(settings_keys):
                        if key in settings_items and settings_items[key].collidepoint(event.pos):
                            settings_focus = index
                            edit_mapping(key)
                            break
                continue
            if event.type == pg.KEYUP and event.scancode == FULLSCREEN_SCANCODE:
                fullscreen_key_held = False
                continue
            if event.type == pg.KEYUP and event.scancode == PAUSE_SCANCODE:
                pause_combo_held = False
                continue
            action = None
            if event.type == pg.KEYDOWN and event.scancode == PAUSE_SCANCODE:
                if ctrl_alt_pause(event.scancode, *modifiers_down(event)) and not pause_combo_held:
                    pause_combo_held = True
                    toggle_fullscreen()
                continue
            if event.type == pg.KEYDOWN and event.scancode == FULLSCREEN_SCANCODE:
                if fullscreen_key_held:
                    continue
                fullscreen_key_held = True
                action = "fullscreen"
            elif event.type == pg.KEYDOWN:
                action = DISPLAY_KEYS.get(event.scancode) or CLIPBOARD_SCANCODES.get(event.scancode)
                if event.scancode == RELEASE_SCANCODE:
                    action = "release"
                if action is None and not captured:
                    if event.key == pg.K_TAB:
                        bar_focus = move_bar_focus(bar_focus, len(BAR_ACTIONS), bool(event.mod & pg.KMOD_SHIFT))
                        continue
                    if bar_focus is not None and event.key in (pg.K_LEFT, pg.K_RIGHT):
                        bar_focus = move_bar_focus(bar_focus, len(BAR_ACTIONS), event.key == pg.K_LEFT)
                        continue
                    if bar_focus is not None and event.key in (pg.K_RETURN, pg.K_SPACE):
                        action = BAR_ACTIONS[bar_focus]
            elif event.type == pg.MOUSEBUTTONDOWN and event.button == 1 and bar_open:
                action = next((key for key, rect in visible.items() if rect.collidepoint(event.pos)), None)
            if action:
                if action == "fullscreen":
                    toggle_fullscreen()
                    continue
                if action == "keymap":
                    release_input()
                    settings_open = True
                    settings_focus = 0
                    bar_focus = None
                if action == "disconnect":
                    release_input()
                    return None, session_size()
                if action in DISPLAY_KEYS.values() and action != mode:
                    release_input()
                    return action, session_size()
                if action == "release":
                    release_input()
                    feedback = ""
                if action in ("send", "fetch"):
                    transfer(action)
                continue
            if event.type == pg.WINDOWFOCUSLOST:
                fullscreen_key_held = False
                pause_combo_held = False
                release_input()
                feedback = ""
                continue
            pointer_y = event.pos[1] if getattr(event, "pos", None) is not None else pg.mouse.get_pos()[1]
            in_bar = event.type in (pg.MOUSEMOTION, pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP, pg.MOUSEWHEEL) and pointer_in_bar(
                bar_open, toolbar_height, pointer_y)
            releasing_held_button = (
                event.type == pg.MOUSEBUTTONUP
                and event.button in MAC_BUTTONS
                and MAC_BUTTONS[event.button] in held_buttons
            )
            if in_bar and not releasing_held_button:
                continue
            if not captured:
                if event.type != pg.MOUSEBUTTONDOWN or event.button != 1 or not area or not area.collidepoint(event.pos):
                    continue
                captured = True
                pg.event.set_grab(True)
                feedback = ""
                continue
            if event.type in (pg.KEYDOWN, pg.KEYUP):
                physical = is_physical_fn(sdl_key_label(event.key), event.scancode)
                code = forwarded_mac_code(event.scancode, mapping, physical)
                if event.type == pg.KEYDOWN:
                    if code is not None:
                        if event.scancode in held_keys or code not in held_keys.values():
                            send("key", code=code, down=True)
                        held_keys[event.scancode] = code
                else:
                    release_key(event.scancode)
            elif event.type == pg.MOUSEMOTION and (area.collidepoint(event.pos) or held_buttons):
                send("move", **point(event.pos))
            elif event.type in (pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP) and event.button in MAC_BUTTONS:
                button = MAC_BUTTONS[event.button]
                down = event.type == pg.MOUSEBUTTONDOWN
                if down and area.collidepoint(event.pos):
                    held_buttons.add(button)
                    now = time.monotonic()
                    last, previous = clicks.get(button, (0, event.pos))
                    nearby = max(abs(a - b) for a, b in zip(event.pos, previous)) <= 5
                    click_counts[button] = min(3, click_counts.get(button, 0) + 1) if now - last < 0.4 and nearby else 1
                    clicks[button] = (now, event.pos)
                    send("button", button=button, down=True, clicks=click_counts[button], **point(event.pos))
                elif not down and button in held_buttons:
                    held_buttons.remove(button)
                    send("button", button=button, down=False, clicks=click_counts[button], **point(event.pos))
            elif event.type == pg.MOUSEWHEEL and area.collidepoint(pg.mouse.get_pos()):
                direction = -1 if event.flipped else 1
                send("scroll", x=event.x * direction, y=event.y * direction)

        # Windows can consume a modifier release while SDL still considers it held.
        # Reconcile only forwarded downs, after processing the entire event batch.
        if captured and physical_key_state is not None:
            for scancode, virtual_key in WINDOWS_MODIFIERS.items():
                if scancode in held_keys and not physical_key_state(virtual_key) & 0x8000:
                    release_key(scancode)

        now = time.monotonic()
        if now - heartbeat >= 0.5:
            send("heartbeat")
            heartbeat = now
        clock.tick(120)
