import json
import tempfile
import unittest
from pathlib import Path

import connect
import desktop_viewer
import viewer


class KeyMappingTests(unittest.TestCase):
    def test_old_mapping_keeps_modifiers_and_defaults_fn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key-mapping.json"
            path.write_text(json.dumps({"226": 58, "224": 59}), encoding="utf-8")
            mapping = desktop_viewer.load_key_mapping(path)
        self.assertEqual(mapping[226], 58)
        self.assertEqual(mapping[224], 59)
        self.assertEqual(mapping["fn"], desktop_viewer.DEFAULT_FN_FALLBACK)
        self.assertEqual(mapping[228], desktop_viewer.MODIFIER_DEFAULTS[228])

    def test_illegal_fn_scancode_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key-mapping.json"
            path.write_text(json.dumps({"fn": 66, "226": 55}), encoding="utf-8")
            mapping = desktop_viewer.load_key_mapping(path)
        self.assertEqual(mapping["fn"], 71)
        self.assertEqual(mapping[226], 55)

    def test_fn_roundtrip_includes_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key-mapping.json"
            mapping = desktop_viewer.default_mapping()
            mapping["fn"] = None
            mapping[226] = 58
            desktop_viewer.save_key_mapping(path, mapping)
            loaded = desktop_viewer.load_key_mapping(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(loaded["fn"])
        self.assertEqual(loaded[226], 58)
        self.assertIsNone(saved["fn"])
        self.assertNotIn("66", saved)
        self.assertEqual(set(saved), {str(key) for key in desktop_viewer.REMAPPABLE_KEYS} | {"fn"})


class ConnectionBarTests(unittest.TestCase):
    def test_bar_open_rules(self):
        open_bar = desktop_viewer.connection_bar_open
        self.assertTrue(open_bar(False, 400, 80, 10, 0, False))
        self.assertTrue(open_bar(True, 0, 80, 10, 0, False))
        self.assertTrue(open_bar(True, 3, 80, 10, 0, False))
        self.assertFalse(open_bar(True, 4, 80, 10, 0, False))
        self.assertTrue(open_bar(True, 20, 80, 10, 0, True))
        self.assertFalse(open_bar(True, 80, 80, 10, 0, True))
        self.assertTrue(open_bar(True, 400, 80, 10, 11, False))

    def test_layout_wraps_a_group_that_does_not_fit(self):
        rects, height, labels = desktop_viewer.layout_connection_bar(
            (("Display", ("mirror", "extend")), ("Session", ("disconnect",))),
            lambda _action: 80,
            200,
        )
        self.assertLess(rects["mirror"][1], rects["disconnect"][1])
        self.assertEqual(rects["mirror"][0], 12)
        self.assertEqual(labels[1][0], "Session")
        self.assertGreater(height, rects["disconnect"][1] + rects["disconnect"][3])

    def test_focus_wraps_and_status_copy(self):
        self.assertEqual(desktop_viewer.move_bar_focus(None, 9, False), 0)
        self.assertEqual(desktop_viewer.move_bar_focus(None, 9, True), 8)
        self.assertEqual(desktop_viewer.move_bar_focus(8, 9, False), 0)
        self.assertFalse(desktop_viewer.pointer_in_bar(False, 80, 2))
        self.assertTrue(desktop_viewer.pointer_in_bar(True, 80, 79))
        self.assertEqual(
            desktop_viewer.connection_status(False, "", False, False),
            "Waiting for the Mac picture.",
        )
        self.assertIn("Ctrl+Alt+Pause", desktop_viewer.connection_status(True, "", True, True))
        self.assertEqual(
            desktop_viewer.connection_status(False, "Clipboard sent to Mac", True, False),
            "Clipboard sent to Mac",
        )
        self.assertEqual(desktop_viewer.button_label("fullscreen", True), "Exit fullscreen")


class InputRoutingTests(unittest.TestCase):
    def test_ctrl_alt_pause(self):
        self.assertTrue(desktop_viewer.ctrl_alt_pause(72, True, True))
        self.assertFalse(desktop_viewer.ctrl_alt_pause(72, True, False))
        self.assertFalse(desktop_viewer.ctrl_alt_pause(66, True, True))

    def test_physical_fn_uses_sdl_name_and_skips_local_keys(self):
        self.assertTrue(desktop_viewer.is_physical_fn("Fn", 71))
        self.assertTrue(desktop_viewer.is_physical_fn(" function ", 101))
        self.assertTrue(desktop_viewer.is_physical_fn("Globe", 40))
        self.assertFalse(desktop_viewer.is_physical_fn("Fn", 66))
        self.assertFalse(desktop_viewer.is_physical_fn("a", 4))
        self.assertFalse(desktop_viewer.fn_label("function key"))

    def test_forwarded_mac_code(self):
        mapping = desktop_viewer.default_mapping()
        self.assertIsNone(desktop_viewer.forwarded_mac_code(66, mapping, True))
        self.assertIsNone(desktop_viewer.forwarded_mac_code(72, mapping, False))
        self.assertEqual(desktop_viewer.forwarded_mac_code(71, mapping, False), 63)
        self.assertEqual(desktop_viewer.forwarded_mac_code(30, mapping, True), 63)
        mapping["fn"] = None
        self.assertIsNone(desktop_viewer.forwarded_mac_code(71, mapping, False))
        self.assertEqual(desktop_viewer.forwarded_mac_code(226, mapping, False), mapping[226])
        self.assertEqual(desktop_viewer.forwarded_mac_code(30, mapping, False), desktop_viewer.MAC_KEYS[30])


class PairingCopyTests(unittest.TestCase):
    def test_prompts_separate_the_pairing_key_from_the_fingerprint(self):
        self.assertIn("Connection", connect.CONNECTION_PROMPT)
        self.assertIn("pairing key", connect.CONNECTION_PROMPT.lower())
        self.assertIn("not the certificate fingerprint", viewer.PAIRING_PROMPT)

    def test_authentication_hint_is_only_for_authentication_failures(self):
        failed = viewer.authentication_failure_text("Host authentication failed")
        self.assertIn("Show paired key", failed)
        self.assertIn("Replace pairing key.cmd", failed)
        self.assertEqual(viewer.authentication_failure_text("Host ended the stream"), "Host ended the stream")


if __name__ == "__main__":
    unittest.main()
