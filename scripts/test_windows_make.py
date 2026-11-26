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


class MingwPyavTests(unittest.TestCase):
    def test_posix_mingw_links_winpthread_statically_and_win32_does_not(self) -> None:
        self.assertEqual(codec.mingw_ldflags("win32"), "-static-libgcc")
        posix = codec.mingw_ldflags("posix")
        self.assertIn("-static-libgcc", posix)
        self.assertIn("-lwinpthread", posix)
        self.assertIn("-Bstatic", posix)
        self.assertNotIn("--enable-pthreads", posix)

    def test_msvc_def_names_the_soname_when_library_is_missing(self) -> None:
        text = codec.msvc_import_def("EXPORTS\n    avcodec_version\n", "avcodec-63.dll")
        self.assertTrue(text.startswith("LIBRARY avcodec-63.dll\n"))
        self.assertIn("avcodec_version", text)
        self.assertNotIn("avcodec.dll\n", text)

    def test_msvc_def_replaces_a_wrong_library_line(self) -> None:
        text = codec.msvc_import_def(
            "LIBRARY libavcodec.dll.a\nEXPORTS\navcodec_version\n",
            "avcodec-63.dll",
        )
        self.assertEqual(text.count("LIBRARY"), 1)
        self.assertIn("LIBRARY avcodec-63.dll\n", text)
        self.assertNotIn("libavcodec.dll.a", text)

    def test_msvc_def_rejects_a_dll_name_that_would_break_the_line(self) -> None:
        with self.assertRaises(SystemExit):
            codec.msvc_import_def("EXPORTS\n", "avcodec-63.dll\nLIBRARY kernel32.dll")


if __name__ == "__main__":
    unittest.main()
