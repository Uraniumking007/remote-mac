"""MSVC FFmpeg make must use the MSYS shell, or awk loses a backslash."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import windows_codec_lgpl as codec


# Characters awk must see. Two backslashes: gsub(/\\/, "/").
KEEP = 'gsub(/\\\\/, "/")'
# One lost backslash, the Actions syntax error: gsub(/\/, "/").
LOST = 'gsub(/\\/, "/")'


class MsysMakeTests(unittest.TestCase):
    def test_awk_keeps_two_backslashes_and_rejects_the_ci_program(self) -> None:
        data = "including file: C:\\foo\\bar.c\n"
        good = (
            '/including/ { sub(/^.*file: */, ""); gsub(/\\\\/, "/"); '
            'if (!match($0, / /)) print "libavdevice/avdevice.o:", $0 }'
        )
        bad = good.replace(KEEP, LOST, 1)
        self.assertIn(LOST, bad)
        self.assertNotIn(KEEP, bad)
        kept = subprocess.run(["awk", good], input=data, text=True, capture_output=True, check=False)
        lost = subprocess.run(["awk", bad], input=data, text=True, capture_output=True, check=False)
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertEqual(kept.stdout, "libavdevice/avdevice.o: C:/foo/bar.c\n")
        self.assertEqual(lost.returncode, 2)
        self.assertIn("syntax error", lost.stderr)

    def test_windows_make_is_msys_and_shell_is_not_git_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "msys64"
            usr = root / "usr" / "bin"
            usr.mkdir(parents=True)
            for name in ("make.exe", "sh.exe", "msys-2.0.dll"):
                (usr / name).write_bytes(b"")
            mingw = Path(tmp) / "mingw64" / "bin"
            mingw.mkdir(parents=True)
            (mingw / "make.exe").write_bytes(b"")
            environ = {
                "PATH": str(mingw) + os.pathsep + r"C:\Program Files\Git\usr\bin",
                "MSYS2_LOCATION": str(root),
                "SHELL": r"C:\Program Files\Git\bin\bash.exe",
                "MSYSTEM": "MSYS",
            }
            argv, env = codec.msys_make_invocation(environ, platform="win32")
        self.assertEqual(argv, [str(root / "usr" / "bin" / "make.exe")])
        self.assertEqual(env["SHELL"], "/usr/bin/sh")
        self.assertNotIn("Git", env["SHELL"])
        self.assertTrue(str(mingw) in env["PATH"])
        self.assertFalse(env["PATH"].startswith(str(usr)))

    def test_missing_msys_make_names_mingw(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            codec.msys_root({"PATH": r"C:\mingw64\bin", "MSYS2_LOCATION": r"C:\missing-msys"})
        self.assertIn(r"C:\mingw64\bin\make.exe", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
