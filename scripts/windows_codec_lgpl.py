#!/usr/bin/env python3
"""Build the pinned LGPL FFmpeg shared libraries used by the Windows viewer.

The PyPI ``av`` wheel is not a usable FFmpeg. Its libavcodec DLL reports
"LGPL version 3" while it links libx264 and libx265. Those encoders are GPL.
This script builds FFmpeg 9.0.2 as shared DLLs with the native H.264 decoder,
without ``--enable-gpl``, ``--enable-nonfree``, or those libraries.

A Mac can cross-compile the DLLs. PyAV has to be compiled on Windows x64 with
MSVC against this prefix. A MinGW ``_av.pyd`` does not load in the embeddable
CPython runtime. The staging directory is not a release prefix until that
Windows build is present. ``scripts/build-windows.py`` refuses to package the
PyPI FFmpeg DLLs either way.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from urllib.request import urlretrieve

ROOT = Path(__file__).resolve().parents[1]
STAGING_DIR = ROOT / "third_party" / "ffmpeg-lgpl-win-x64"
CACHE_DIR = ROOT / "third_party" / "cache"

FFMPEG_VERSION = "9.0.2"
FFMPEG_TARBALL = f"ffmpeg-{FFMPEG_VERSION}.tar.xz"
FFMPEG_URL = f"https://ffmpeg.org/releases/{FFMPEG_TARBALL}"
FFMPEG_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
FFMPEG_LICENSE_SHA256 = "2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af"
FFMPEG_COPYING_SHA256 = "246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30"

PYAV_VERSION = "19.0.2"
PYAV_TARBALL = f"av-{PYAV_VERSION}.tar.gz"
PYAV_URL = f"https://files.pythonhosted.org/packages/source/a/av/{PYAV_TARBALL}"
PYAV_SHA256 = "bebbc52598886480f4d861c575dc2b020785d6c04e1308680ddcc545e1b94c66"
PYAV_LICENSE_SHA256 = "76af0461ffb92e19f1c14449e95557d83a2dfaa1baf202d49e5f1d8746c0da19"
PYAV_WHEEL_SHA256 = "3ce5a1d7b916d13c16ac58f1756f0fee4af2d53f02bc1403e18550b50d375a22"

# These tokens are the license and feature pin. Host flags (--prefix, the
# cross compiler, --toolchain) stay off this list so a Windows MSVC build can
# match a MinGW cross build. --extra-ldflags=-static-libgcc is MinGW-only:
# link.exe does not take that flag. The gate rejects libgcc_s_seh-1.dll instead.
LICENSE_TOKENS = (
    "--disable-autodetect",
    "--enable-shared",
    "--disable-static",
    "--disable-gpl",
    "--disable-nonfree",
    "--disable-version3",
    "--disable-programs",
    "--disable-doc",
    "--disable-debug",
    "--disable-pthreads",
    "--enable-w32threads",
    "--disable-iconv",
    "--disable-zlib",
    "--disable-bzlib",
    "--disable-lzma",
    "--disable-sdl2",
    "--disable-libx264",
    "--disable-libx265",
    "--disable-libxvid",
    "--disable-libxavs",
    "--disable-libxavs2",
    "--disable-avisynth",
    "--disable-frei0r",
    "--disable-libcdio",
    "--disable-libdavs2",
    "--disable-librubberband",
    "--disable-libvidstab",
    "--disable-libfdk-aac",
    "--disable-openssl",
    "--disable-libopencore-amrnb",
    "--disable-libopencore-amrwb",
    "--disable-libvo-amrwbenc",
    "--disable-liblensfun",
    "--disable-libvmaf",
    "--disable-mbedtls",
    "--disable-gmp",
    "--disable-libaribb24",
    "--disable-libmp3lame",
    "--disable-libopus",
    "--disable-libvpx",
    "--disable-libdav1d",
    "--disable-libsvtav1",
    "--disable-libwebp",
    "--disable-mediafoundation",
    "--disable-amf",
    "--disable-cuvid",
    "--disable-d3d11va",
    "--disable-d3d12va",
    "--disable-dxva2",
    "--disable-ffnvcodec",
    "--disable-nvdec",
    "--disable-nvenc",
    "--disable-vaapi",
    "--disable-vdpau",
    "--disable-videotoolbox",
    "--disable-vulkan",
    "--disable-cuda-llvm",
    "--disable-x86asm",
    "--enable-decoder=h264",
    "--enable-parser=h264",
)

FORBIDDEN_TOKENS = (
    "--enable-gpl",
    "--enable-nonfree",
    "--enable-version3",
    "--enable-libx264",
    "--enable-libx265",
    "--enable-libxvid",
    "--enable-libxavs",
    "--enable-libxavs2",
    "--enable-avisynth",
    "--enable-frei0r",
    "--enable-libcdio",
    "--enable-libdavs2",
    "--enable-librubberband",
    "--enable-libvidstab",
    "--enable-libfdk-aac",
    "--enable-openssl",
    "--enable-libopencore-amrnb",
    "--enable-libopencore-amrwb",
    "--enable-libvo-amrwbenc",
    "--enable-libvmaf",
    "--enable-mbedtls",
    "--enable-libmp3lame",
    "--enable-libopus",
    "--enable-libvpx",
    "--enable-libdav1d",
    "--enable-libsvtav1",
    "--enable-libwebp",
)

SONAME_DLLS = (
    "avcodec-63.dll",
    "avdevice-63.dll",
    "avfilter-12.dll",
    "avformat-63.dll",
    "avutil-61.dll",
    "swresample-7.dll",
    "swscale-10.dll",
)

REQUIRED_CONFIG_H = {
    "CONFIG_GPL": "0",
    "CONFIG_NONFREE": "0",
    "CONFIG_VERSION3": "0",
    "CONFIG_LIBX264": "0",
    "CONFIG_LIBX265": "0",
    "CONFIG_LIBXVID": "0",
    "CONFIG_SHARED": "1",
    "HAVE_W32THREADS": "1",
    "FFMPEG_LICENSE": "LGPL version 2.1 or later",
}

REQUIRED_COMPONENTS = {
    "CONFIG_H264_DECODER": "1",
    "CONFIG_H264_PARSER": "1",
    "CONFIG_LIBX264_ENCODER": "0",
    "CONFIG_LIBX265_ENCODER": "0",
    "CONFIG_LIBXVID_ENCODER": "0",
    "CONFIG_LIBOPENH264_DECODER": "0",
    "CONFIG_LIBFDK_AAC_DECODER": "0",
    "CONFIG_LIBFDK_AAC_ENCODER": "0",
}

MISSING_PREFIX_MESSAGE = (
    "Windows release has no LGPL staging dir; build with the script on Windows x64 "
    "(MSVC PyAV against pinned shared FFmpeg); this process did not run Windows playback "
    "or confirm MSVC/MinGW ABI; refusing to package PyPI av FFmpeg DLLs."
)

NOTICE_TEXT = """\
Remote Mac uses FFmpeg under the GNU Lesser General Public License version 2.1 or later.
FFmpeg is a trademark of Fabrice Bellard.
https://ffmpeg.org/legal.html

This build does not pass --enable-gpl or --enable-nonfree.
It does not link libx264, libx265, or other GPL or incompatible libraries.
The native H.264 decoder is part of this LGPL build.
H.264 patents may apply to use of that decoder. This notice is not a legal opinion.

No FFmpeg files were modified. There are no local patches.
FFmpeg source, the configure line, config.h, and the license text are in third-party/ffmpeg.
The source tarball in that directory is the exact corresponding source for the DLLs next to python.exe.
The DLLs stay separate files. Nothing renames avcodec-63.dll to a content hash.

Parts of the JPEG IDCT are derived from the Independent JPEG Group and are unmodified:
jfdctfst.c, jfdctint_template.c, and jrevdct.c.

PyAV is under the BSD 3-Clause License. Its license is in third-party/pyav.
This PyAV is dynamically linked to these FFmpeg DLLs.
It is not the PyPI av wheel. Do not replace these DLLs with av.libs from that wheel.
That wheel links libx264 and libx265 while its libavcodec library reports LGPL version 3.
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_file(url: str, dest: Path, expected: str) -> None:
    if dest.is_file() and sha256_file(dest) == expected:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    urlretrieve(url, partial)
    got = sha256_file(partial)
    if got != expected:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"{dest.name} SHA-256 {got} does not match {expected}.")
    partial.replace(dest)


def extract_tarball(tarball: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    with tarfile.open(tarball) as archive:
        archive.extractall(destination, **kwargs)


def filtered_requirements(path: Path) -> str:
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "av" or stripped.startswith("av==") or stripped.startswith("av "):
            continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def _host_flags(prefix: Path) -> list[str]:
    flags = [f"--prefix={prefix.as_posix()}"]
    if sys.platform == "win32":
        flags.extend(["--toolchain=msvc", "--arch=x86_64", "--target-os=win32"])
        return flags
    gcc = shutil.which("x86_64-w64-mingw32-gcc")
    if gcc is None:
        raise SystemExit("Windows x64 MinGW compiler not found: x86_64-w64-mingw32-gcc")
    flags.extend([
        "--enable-cross-compile",
        "--arch=x86_64",
        "--target-os=mingw32",
        "--cross-prefix=x86_64-w64-mingw32-",
        "--extra-ldflags=-static-libgcc",
    ])
    return flags


def _prepare_msys_env() -> None:
    """Keep the Windows PATH order when FFmpeg's configure runs under MSYS bash."""
    if sys.platform != "win32":
        return
    os.environ.setdefault("MSYS2_PATH_TYPE", "inherit")
    os.environ.setdefault("MSYSTEM", "MSYS")


def build_ffmpeg() -> None:
    _prepare_msys_env()
    bash = shutil.which("bash")
    if bash is None:
        raise SystemExit("bash is required to run FFmpeg's configure script.")
    if shutil.which("make") is None:
        raise SystemExit("make is not on PATH. FFmpeg's build needs GNU make (MSYS2 make on Windows).")
    if sys.platform == "win32" and shutil.which("cl") is None:
        raise SystemExit(
            "cl.exe is not on PATH. Start the x64 Native Tools shell for Visual Studio, "
            "then run this from bash so FFmpeg's configure script can run."
        )
    tarball = CACHE_DIR / FFMPEG_TARBALL
    ensure_file(FFMPEG_URL, tarball, FFMPEG_SHA256)
    source_parent = CACHE_DIR / "src" / "ffmpeg"
    extract_tarball(tarball, source_parent)
    source = source_parent / f"ffmpeg-{FFMPEG_VERSION}"
    flags = [*_host_flags(STAGING_DIR), *LICENSE_TOKENS]
    configure = source / "configure"
    subprocess.run([bash, str(configure), *flags], cwd=source, check=True)
    jobs = str(os.cpu_count() or 1)
    subprocess.run(["make", f"-j{jobs}"], cwd=source, check=True)
    subprocess.run(["make", "install"], cwd=source, check=True)
    legal = STAGING_DIR / "legal" / "ffmpeg"
    legal.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE.md", "COPYING.LGPLv2.1", "config.h", "config_components.h"):
        shutil.copy2(source / name, legal / name)
    shutil.copy2(source / "ffbuild" / "config.log", legal / "config.log")
    shutil.copy2(tarball, legal / FFMPEG_TARBALL)
    copying = sha256_file(legal / "COPYING.LGPLv2.1")
    license_md = sha256_file(legal / "LICENSE.md")
    if copying != FFMPEG_COPYING_SHA256 or license_md != FFMPEG_LICENSE_SHA256:
        raise SystemExit("Installed FFmpeg license files do not match the pinned hashes.")
    (STAGING_DIR / "legal" / "NOTICE.txt").write_text(NOTICE_TEXT, encoding="utf-8")
    toolchain = "MSVC" if sys.platform == "win32" else "MinGW-w64 cross compile from " + sys.platform
    build_text = "\n".join([
        f"FFmpeg version {FFMPEG_VERSION}",
        f"source sha256 {FFMPEG_SHA256}",
        f"source url {FFMPEG_URL}",
        f"COPYING.LGPLv2.1 sha256 {FFMPEG_COPYING_SHA256}",
        f"LICENSE.md sha256 {FFMPEG_LICENSE_SHA256}",
        "No local patches.",
        f"Toolchain: {toolchain}",
        "configure: " + " ".join(flags),
        "Shared DLLs only. Dynamic linking. SONAMEs are avcodec-63, avutil-61, swscale-10, "
        "avformat-63, avdevice-63, avfilter-12, and swresample-7.",
        "x86 assembly is disabled (--disable-x86asm) so the pin does not assume nasm is installed. "
        "The native C H.264 decoder is still enabled. A faster build installs nasm, passes "
        "--enable-x86asm instead, and updates this pin and scripts/verify_windows_codecs.py together.",
        "MinGW links with --extra-ldflags=-static-libgcc and --enable-w32threads so the DLLs do not "
        "depend on libgcc_s_seh-1.dll or libwinpthread-1.dll. An MSVC build omits the libgcc flag.",
        "This tree does not claim the MSVC and MinGW DLL ABIs match until PyAV is built and loaded "
        "on Windows. No Windows playback test is run by this script.",
        "",
    ])
    (legal / "BUILD.txt").write_text(build_text, encoding="utf-8")
    _assert_ffmpeg_bin()
    if sys.platform != "win32":
        print(
            "FFmpeg DLLs are in third_party/ffmpeg-lgpl-win-x64/bin. "
            "PyAV was not built. This prefix is not a Windows release staging directory.",
            file=sys.stderr,
        )


def _assert_ffmpeg_bin() -> None:
    from verify_windows_codecs import codec_dll_errors, is_banned_codec_dll, pe_import_dlls

    banned_runtime = {"libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll"}
    for name in SONAME_DLLS:
        if not (STAGING_DIR / "bin" / name).is_file():
            raise SystemExit(f"FFmpeg install is missing bin/{name}.")
    dll = STAGING_DIR / "bin" / "avcodec-63.dll"
    data = dll.read_bytes()
    errors = codec_dll_errors(data, dll.name)
    try:
        imports = pe_import_dlls(data)
    except Exception as exc:
        raise SystemExit(f"Could not read imports from {dll.name}: {exc}") from exc
    lowered = {item.lower() for item in imports}
    if "avutil-61.dll" not in lowered:
        errors.append("avcodec-63.dll does not import avutil-61.dll.")
    for item in imports:
        if is_banned_codec_dll(item) or item.lower() in banned_runtime:
            errors.append(f"avcodec-63.dll imports {item}.")
    for path in (STAGING_DIR / "bin").glob("*.dll"):
        if path.name.lower() in banned_runtime or is_banned_codec_dll(path.name):
            errors.append(f"install shipped {path.name}.")
    if errors:
        raise SystemExit("FFmpeg build failed the license check:\n" + "\n".join(errors))


def _msvc_import_libs() -> None:
    lib_tool = shutil.which("lib")
    if lib_tool is None:
        raise SystemExit(
            "lib.exe is not on PATH. PyAV's MSVC link needs import libraries named avcodec.lib "
            "and the other library names. Generate them from the .def files with lib.exe."
        )
    library_dir = STAGING_DIR / "lib"
    for name in SONAME_DLLS:
        stem = name[: -len(".dll")]
        definition = library_dir / f"{stem}.def"
        if not definition.is_file():
            raise SystemExit(f"Missing {definition}. The shared FFmpeg install did not export a .def file.")
        output = library_dir / f"{stem.split('-')[0]}.lib"
        subprocess.run(
            [lib_tool, "/nologo", "/MACHINE:X64", f"/DEF:{definition}", f"/OUT:{output}"],
            cwd=library_dir,
            check=True,
        )


def build_pyav() -> None:
    _prepare_msys_env()
    if sys.platform != "win32":
        raise SystemExit(
            "Refusing to build PyAV here. FFmpeg DLLs can be cross-compiled, but PyAV "
            f"{PYAV_VERSION} must be built with MSVC on Windows x64 against {STAGING_DIR}. "
            "A MinGW _av.pyd is not ABI-compatible with python.org CPython. "
            "MinGW writes libavcodec.dll.a, not an MSVC avcodec.lib. On Windows, this script "
            "runs lib.exe /DEF:avcodec-63.def /OUT:avcodec.lib /MACHINE:X64 before setup.py. "
            "This process did not run Windows playback or confirm the MSVC/MinGW DLL ABI."
        )
    if sys.version_info[:2] != (3, 13):
        raise SystemExit(
            "Build PyAV with CPython 3.13 so the limited-API extension matches the embeddable "
            f"runtime. This interpreter is {sys.version.split()[0]}."
        )
    try:
        import Cython  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "The PyAV sdist has no generated extension .c files. Install Cython in this "
            "interpreter: python -m pip install cython setuptools"
        ) from exc
    if not (STAGING_DIR / "bin" / "avcodec-63.dll").is_file():
        raise SystemExit(MISSING_PREFIX_MESSAGE)
    _msvc_import_libs()
    tarball = CACHE_DIR / PYAV_TARBALL
    ensure_file(PYAV_URL, tarball, PYAV_SHA256)
    source_parent = CACHE_DIR / "src" / "pyav"
    extract_tarball(tarball, source_parent)
    source = source_parent / f"av-{PYAV_VERSION}"
    subprocess.run(
        [sys.executable, "setup.py", "build_ext", "--inplace", f"--ffmpeg-dir={STAGING_DIR}"],
        cwd=source,
        check=True,
    )
    built = source / "av"
    pyds = list(built.rglob("*.pyd"))
    if not pyds:
        raise SystemExit("PyAV build_ext finished without a .pyd. The extension did not link.")
    destination = STAGING_DIR / "pyav" / "site-packages" / "av"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(built, destination)
    legal = STAGING_DIR / "legal" / "pyav"
    legal.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "LICENSE.txt", legal / "LICENSE.txt")
    shutil.copy2(tarball, legal / PYAV_TARBALL)
    if sha256_file(legal / "LICENSE.txt") != PYAV_LICENSE_SHA256:
        raise SystemExit("PyAV LICENSE.txt does not match the pinned hash.")
    (legal / "BUILD.txt").write_text(
        "\n".join([
            f"PyAV {PYAV_VERSION}",
            f"source sha256 {PYAV_SHA256}",
            f"source url {PYAV_URL}",
            f"LICENSE.txt sha256 {PYAV_LICENSE_SHA256}",
            "Dynamically linked to the FFmpeg DLLs in this prefix.",
            "Not the PyPI wheel.",
            f"The rejected wheel sha256 is {PYAV_WHEEL_SHA256}. That wheel is not redistributed.",
            f"Command: {sys.executable} setup.py build_ext --inplace --ffmpeg-dir={STAGING_DIR}",
            "Import libraries were created with lib.exe from the FFmpeg .def files.",
            "",
        ]),
        encoding="utf-8",
    )
    print(f"PyAV staged under {destination}.")


def _remove_packaged_pyav(packages: Path) -> None:
    if not packages.is_dir():
        return
    for child in packages.iterdir():
        name = child.name
        if name in {"av", "av.libs"} or (name.startswith("av-") and name.endswith(".dist-info")):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


def stage_lgpl_into_bundle(packages: Path, bundle: Path) -> None:
    """Copy a completed staging prefix into the Windows bundle. Fail closed otherwise."""
    pyds = list((STAGING_DIR / "pyav" / "site-packages").rglob("*.pyd")) if STAGING_DIR.is_dir() else []
    dll = STAGING_DIR / "bin" / "avcodec-63.dll"
    if not dll.is_file() or not pyds:
        raise SystemExit(MISSING_PREFIX_MESSAGE)
    runtime = bundle / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _remove_packaged_pyav(packages)
    for path in (STAGING_DIR / "bin").glob("*.dll"):
        shutil.copy2(path, runtime / path.name)
    site = STAGING_DIR / "pyav" / "site-packages"
    for child in site.iterdir():
        target = packages / child.name
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    legal = STAGING_DIR / "legal"
    shutil.copy2(legal / "NOTICE.txt", bundle / "NOTICE.txt")
    for name in ("ffmpeg", "pyav"):
        destination = bundle / "third-party" / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(legal / name, destination)
    assert_release_bundle(bundle)


def assert_release_bundle(bundle: Path) -> None:
    from verify_windows_codecs import verify_bundle

    errors = verify_bundle(bundle)
    if errors:
        raise SystemExit("Windows codec license gate failed:\n" + "\n".join(f"- {item}" for item in errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("ffmpeg", "pyav", "all"))
    args = parser.parse_args()
    if args.step in {"ffmpeg", "all"}:
        build_ffmpeg()
    if args.step in {"pyav", "all"}:
        build_pyav()


if __name__ == "__main__":
    main()
