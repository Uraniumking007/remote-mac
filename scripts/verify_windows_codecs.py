#!/usr/bin/env python3
"""Fail closed unless a Windows bundle contains the pinned LGPL FFmpeg DLLs.

The check reads the files that would ship. A license string by itself is not
enough: the PyPI av wheel says "LGPL version 3" and still links libx264.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import struct
import sys
import tempfile
from pathlib import Path
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import windows_codec_lgpl as pin

LICENSE_STRING = b"libavcodec license: LGPL version 2.1 or later"
VERSION_STRING = b"FFmpeg version 9.0.2"
H264_STRING = b"H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10"
LGPL3_STRING = b"LGPL version 3"

SYSTEM_DLLS = {
    "kernel32.dll",
    "user32.dll",
    "gdi32.dll",
    "advapi32.dll",
    "shell32.dll",
    "ole32.dll",
    "oleaut32.dll",
    "comdlg32.dll",
    "comctl32.dll",
    "ws2_32.dll",
    "winmm.dll",
    "avicap32.dll",
    "mf.dll",
    "mfplat.dll",
    "mfreadwrite.dll",
    "imm32.dll",
    "version.dll",
    "setupapi.dll",
    "bcrypt.dll",
    "crypt32.dll",
    "ntdll.dll",
    "rpcrt4.dll",
    "shlwapi.dll",
    "uxtheme.dll",
    "dwmapi.dll",
    "d3d11.dll",
    "dxgi.dll",
    "opengl32.dll",
    "glu32.dll",
    "winspool.drv",
    "msimg32.dll",
    "usp10.dll",
    "dnsapi.dll",
    "iphlpapi.dll",
    "mpr.dll",
    "netapi32.dll",
    "userenv.dll",
    "wtsapi32.dll",
    "psapi.dll",
    "powrprof.dll",
    "winhttp.dll",
    "secur32.dll",
    "sspicli.dll",
    "cfgmgr32.dll",
    "hid.dll",
    "mmdevapi.dll",
    "propsys.dll",
    "shcore.dll",
    "windows.storage.dll",
    "msvcrt.dll",
    "ucrtbase.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "concrt140.dll",
}

BANNED_RUNTIME = {
    "libgcc_s_seh-1.dll",
    "libwinpthread-1.dll",
    "libstdc++-6.dll",
}

NOTICE_MARKERS = (
    "FFmpeg",
    "Lesser General Public License version 2.1",
    "third-party/ffmpeg",
    "native H.264",
    "jfdctfst.c",
    "jfdctint_template.c",
    "jrevdct.c",
    "PyAV",
    "BSD",
    "av.libs",
    "libx264",
    "Fabrice Bellard",
    "not a legal opinion",
    "dynamically linked",
    "No FFmpeg files were modified",
)

HASHED_DLL = re.compile(
    r"(?i)^(avcodec|avutil|avformat|avdevice|avfilter|swscale|swresample)-\d+-[0-9a-f]{8,}\.dll$"
)
DEFINE_LINE = re.compile(r"^\s*#define\s+([A-Za-z0-9_]+)\s+(.*?)\s*$")


class PeError(Exception):
    pass


def is_banned_codec_dll(name: str) -> bool:
    lowered = name.lower()
    if lowered in {"x264.dll", "x265.dll"}:
        return True
    return lowered.startswith((
        "libx264",
        "libx265",
        "libxvid",
        "libxavs",
        "libfdk",
        "libdavs2",
    ))


def is_system_dll(name: str) -> bool:
    lowered = name.lower()
    if lowered in SYSTEM_DLLS:
        return True
    return lowered.startswith(("api-ms-win-", "ext-ms-"))


def _align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def _cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\x00", offset, offset + 512)
    if end < 0:
        raise PeError(f"unterminated string at {offset:#x}")
    return data[offset:end].decode("ascii", "replace")


def _rva_to_offset(sections: list[tuple[int, int, int]], rva: int) -> int:
    for virtual_address, raw_pointer, span in sections:
        if virtual_address <= rva < virtual_address + span:
            return raw_pointer + (rva - virtual_address)
    raise PeError(f"RVA {rva:#x} is not in a section")


def _sections(data: bytes, section_offset: int, count: int) -> list[tuple[int, int, int]]:
    found = []
    for index in range(count):
        base = section_offset + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
        span = max(virtual_size, raw_size, 1)
        found.append((virtual_address, raw_pointer, span))
    return found


def pe_import_dlls(data: bytes) -> list[str]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PeError("not a PE file")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise PeError("bad PE signature")
    coff = e_lfanew + 4
    machine, section_count, _, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, coff)
    if machine != 0x8664:
        raise PeError(f"machine {machine:#x} is not x64")
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:
        number_offset = optional + 108
        directory_offset = optional + 112
        thunk_format = "<Q"
        thunk_size = 8
        ordinal_flag = 1 << 63
    elif magic == 0x10B:
        number_offset = optional + 92
        directory_offset = optional + 96
        thunk_format = "<I"
        thunk_size = 4
        ordinal_flag = 1 << 31
    else:
        raise PeError(f"optional magic {magic:#x}")
    directory_count = struct.unpack_from("<I", data, number_offset)[0]
    sections = _sections(data, optional + optional_size, section_count)
    names: list[str] = []
    if directory_count > 1:
        import_rva, import_size = struct.unpack_from("<II", data, directory_offset + 8)
        if import_rva and import_size:
            names.extend(_import_descriptors(data, sections, import_rva, thunk_format, thunk_size, ordinal_flag))
    if directory_count > 13:
        delay_rva, delay_size = struct.unpack_from("<II", data, directory_offset + 13 * 8)
        if delay_rva and delay_size:
            names.extend(_delay_descriptors(data, sections, delay_rva))
    return names


def _import_descriptors(data, sections, import_rva, thunk_format, thunk_size, ordinal_flag) -> list[str]:
    names = []
    offset = _rva_to_offset(sections, import_rva)
    while offset + 20 <= len(data):
        lookup, _, _, name_rva, address = struct.unpack_from("<IIIII", data, offset)
        if name_rva == 0 and lookup == 0 and address == 0:
            break
        names.append(_cstring(data, _rva_to_offset(sections, name_rva)))
        offset += 20
        # Touch the name table so a truncated thunk is an error, not a silent pass.
        thunk_rva = lookup or address
        if thunk_rva == 0:
            raise PeError("import descriptor has no thunk table")
        thunk = _rva_to_offset(sections, thunk_rva)
        while thunk + thunk_size <= len(data):
            value = struct.unpack_from(thunk_format, data, thunk)[0]
            if value == 0:
                break
            if not value & ordinal_flag:
                _cstring(data, _rva_to_offset(sections, value & (ordinal_flag - 1)) + 2)
            thunk += thunk_size
    return names


def _delay_descriptors(data, sections, delay_rva) -> list[str]:
    names = []
    offset = _rva_to_offset(sections, delay_rva)
    while offset + 32 <= len(data):
        attributes, name_rva = struct.unpack_from("<II", data, offset)
        if name_rva == 0:
            break
        if attributes & 1 == 0:
            raise PeError("delay-load directory is not RVA-based; refusing to ignore it")
        names.append(_cstring(data, _rva_to_offset(sections, name_rva)))
        offset += 32
    return names


def _cstring_around(data: bytes, index: int) -> str | None:
    begin = data.rfind(b"\x00", 0, index) + 1
    end = data.find(b"\x00", index)
    if end < 0 or end - begin > 20000:
        return None
    return data[begin:end].decode("ascii", "replace")


def configure_string(data: bytes) -> str | None:
    """Return the embedded configure line.

    The PyPI wheel's line has ``--enable-libx264`` and does not contain
    ``--enable-gpl`` or ``--disable-gpl``. Search ``--disable-static`` first,
    which both that wheel and the pinned build include.
    """
    best = None
    for needle in (b"--disable-static", b"--enable-libx264", b"--enable-gpl", b"--disable-gpl"):
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            text = _cstring_around(data, index)
            if text and (best is None or len(text) > len(best)):
                best = text
            start = index + 1
        if needle == b"--disable-static" and best and len(best) > 100:
            return best
    return best


def _tokens(text: str) -> set[str]:
    return set(text.split())


def _token_base(token: str) -> str:
    return token.split("=", 1)[0]


def codec_dll_errors(data: bytes, filename: str) -> list[str]:
    errors = []
    if Path(filename).name.lower() != "avcodec-63.dll":
        errors.append(f"{filename} is not the unhashed SONAME avcodec-63.dll")
    if HASHED_DLL.match(Path(filename).name):
        errors.append(f"{filename} uses a content hash in the DLL name")
    if LICENSE_STRING not in data:
        errors.append(f"{filename} is missing {LICENSE_STRING.decode()}")
    if LGPL3_STRING in data:
        errors.append(f"{filename} reports LGPL version 3")
    if VERSION_STRING not in data:
        errors.append(f"{filename} is missing {VERSION_STRING.decode()}")
    if H264_STRING not in data:
        errors.append(f"{filename} is missing the native H.264 decoder name")
    text = configure_string(data)
    if text is None:
        errors.append(f"{filename} has no embedded configure line")
        return errors
    present = _tokens(text)
    present_bases = {_token_base(item) for item in present}
    for token in pin.LICENSE_TOKENS:
        if token not in present and _token_base(token) not in present_bases:
            errors.append(f"{filename} configure line is missing {token}")
    for token in pin.FORBIDDEN_TOKENS:
        if any(item == token or item.startswith(token + "=") for item in present):
            errors.append(f"{filename} configure line contains {token}")
    return errors


def _defines(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        match = DEFINE_LINE.match(line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == '"' and value.endswith('"'):
            value = value[1:-1]
        found[match.group(1)] = value
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(path: Path, errors: list[str]) -> bool:
    if path.is_file():
        return True
    errors.append(f"missing {path}")
    return False


def verify_bundle(bundle: Path) -> list[str]:
    errors: list[str] = []
    bundle = bundle.resolve()
    runtime = bundle / "runtime"
    for name in pin.SONAME_DLLS:
        path = runtime / name
        if not path.is_file():
            errors.append(f"missing runtime/{name}")
    avcodec = runtime / "avcodec-63.dll"
    if avcodec.is_file():
        data = avcodec.read_bytes()
        errors.extend(codec_dll_errors(data, avcodec.name))
    notice = bundle / "NOTICE.txt"
    if _require(notice, errors):
        text = notice.read_text(encoding="utf-8")
        for marker in NOTICE_MARKERS:
            if marker not in text:
                errors.append(f"NOTICE.txt is missing {marker!r}")
    legal = bundle / "third-party" / "ffmpeg"
    copying = legal / "COPYING.LGPLv2.1"
    license_md = legal / "LICENSE.md"
    tarball = legal / pin.FFMPEG_TARBALL
    config_h = legal / "config.h"
    components = legal / "config_components.h"
    config_log = legal / "config.log"
    build_txt = legal / "BUILD.txt"
    for path in (copying, license_md, tarball, config_h, components, config_log, build_txt):
        _require(path, errors)
    if copying.is_file() and _sha256(copying) != pin.FFMPEG_COPYING_SHA256:
        errors.append("COPYING.LGPLv2.1 hash does not match the FFmpeg 9.0.2 tarball")
    if license_md.is_file() and _sha256(license_md) != pin.FFMPEG_LICENSE_SHA256:
        errors.append("LICENSE.md hash does not match the FFmpeg 9.0.2 tarball")
    if tarball.is_file() and _sha256(tarball) != pin.FFMPEG_SHA256:
        errors.append("ffmpeg source tarball hash does not match the pin")
    if config_h.is_file():
        found = _defines(config_h.read_text(encoding="utf-8", errors="replace"))
        for key, value in pin.REQUIRED_CONFIG_H.items():
            if found.get(key) != value:
                errors.append(f"config.h {key} is {found.get(key)!r}, expected {value!r}")
        configuration = found.get("FFMPEG_CONFIGURATION", "")
        for token in pin.LICENSE_TOKENS:
            if token not in configuration.split():
                errors.append(f"config.h FFMPEG_CONFIGURATION is missing {token}")
    if components.is_file():
        found = _defines(components.read_text(encoding="utf-8", errors="replace"))
        for key, value in pin.REQUIRED_COMPONENTS.items():
            if found.get(key) != value:
                errors.append(f"config_components.h {key} is {found.get(key)!r}, expected {value!r}")
    if config_log.is_file():
        log_text = config_log.read_text(encoding="utf-8", errors="replace")
        for token in pin.LICENSE_TOKENS:
            if token not in log_text.split():
                errors.append(f"config.log is missing {token}")
    if build_txt.is_file():
        build_text = build_txt.read_text(encoding="utf-8")
        for marker in (
            f"FFmpeg version {pin.FFMPEG_VERSION}",
            pin.FFMPEG_SHA256,
            "No local patches.",
            "--disable-x86asm",
        ):
            if marker not in build_text:
                errors.append(f"ffmpeg BUILD.txt is missing {marker!r}")
    pyav = bundle / "third-party" / "pyav"
    pyav_tarball = pyav / pin.PYAV_TARBALL
    pyav_license = pyav / "LICENSE.txt"
    pyav_build = pyav / "BUILD.txt"
    for path in (pyav_tarball, pyav_license, pyav_build):
        _require(path, errors)
    if pyav_tarball.is_file() and _sha256(pyav_tarball) != pin.PYAV_SHA256:
        errors.append("PyAV sdist hash does not match the pin")
    if pyav_license.is_file() and _sha256(pyav_license) != pin.PYAV_LICENSE_SHA256:
        errors.append("PyAV LICENSE.txt hash does not match the pin")
    if pyav_build.is_file():
        text = pyav_build.read_text(encoding="utf-8")
        for marker in (f"PyAV {pin.PYAV_VERSION}", pin.PYAV_SHA256, "Not the PyPI wheel", "Dynamically linked"):
            if marker not in text:
                errors.append(f"PyAV BUILD.txt is missing {marker!r}")
    if (bundle / "av.libs").exists() or any(bundle.rglob("av.libs")):
        errors.append("bundle contains av.libs from the PyPI wheel")
    pyds = list((bundle / "runtime" / "site-packages" / "av").rglob("*.pyd")) if bundle.is_dir() else []
    if not pyds:
        errors.append("no PyAV .pyd is packaged")
    imported: set[str] = set()
    avcodec_importers = 0
    for path in pyds:
        blob = path.read_bytes()
        if LICENSE_STRING in blob or b"libavcodec license:" in blob:
            errors.append(f"{path.name} contains libavcodec; that looks statically linked")
        try:
            dlls = pe_import_dlls(blob)
        except PeError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        lowered = {item.lower() for item in dlls}
        imported.update(lowered)
        if "avcodec-63.dll" in lowered:
            avcodec_importers += 1
    if pyds and avcodec_importers == 0:
        errors.append("no PyAV .pyd imports avcodec-63.dll")
    for required in ("avcodec-63.dll", "avutil-61.dll", "swscale-10.dll"):
        if pyds and required not in imported:
            errors.append(f"PyAV .pyd imports do not include {required}")
    present = {}
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        present.setdefault(path.name.lower(), path)
        if HASHED_DLL.match(path.name) or is_banned_codec_dll(path.name) or path.name.lower() in BANNED_RUNTIME:
            errors.append(f"banned file {path.relative_to(bundle)}")
        if path.suffix.lower() == ".dll" and path.name.lower().startswith("avcodec") and path.name.lower() != "avcodec-63.dll":
            errors.append(f"unexpected avcodec DLL {path.name}")
    for path in bundle.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".dll", ".pyd", ".exe"}:
            continue
        try:
            dlls = pe_import_dlls(path.read_bytes())
        except PeError as exc:
            errors.append(f"{path.relative_to(bundle)}: {exc}")
            continue
        for dll in dlls:
            lowered = dll.lower()
            if is_banned_codec_dll(dll) or lowered in BANNED_RUNTIME:
                errors.append(f"{path.name} imports {dll}")
            elif not is_system_dll(dll) and lowered not in present:
                errors.append(f"{path.name} imports {dll}, which is not in the bundle")
    return errors


def wheel_problems(path: Path) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        codec_dlls = [name for name in names if name.lower().endswith(".dll") and "avcodec" in Path(name).name.lower()]
        if not codec_dlls:
            problems.append("wheel has no avcodec DLL")
        for name in names:
            base = Path(name).name
            if HASHED_DLL.match(base) or is_banned_codec_dll(base):
                problems.append(f"wheel ships {name}")
        for name in codec_dlls:
            data = archive.read(name)
            problems.extend(codec_dll_errors(data, Path(name).name))
            try:
                for dll in pe_import_dlls(data):
                    if is_banned_codec_dll(dll):
                        problems.append(f"{name} imports {dll}")
            except PeError as exc:
                problems.append(f"{name}: {exc}")
    return problems


def _synthetic_dll(imports: list[tuple[str, list[str]]], payload: bytes) -> bytes:
    """Minimal PE32+ DLL whose import table this module can read back."""
    idata = bytearray((len(imports) + 1) * 20)
    meta = []
    for dll_name, functions in imports:
        lookup = len(idata)
        idata.extend(b"\x00" * (8 * (len(functions) + 1)))
        address = len(idata)
        idata.extend(b"\x00" * (8 * (len(functions) + 1)))
        hints = []
        for function in functions:
            if len(idata) % 2:
                idata.append(0)
            hints.append(len(idata))
            idata += struct.pack("<H", 0) + function.encode("ascii") + b"\x00"
        if len(idata) % 2:
            idata.append(0)
        name_at = len(idata)
        idata += dll_name.encode("ascii") + b"\x00"
        meta.append((lookup, address, hints, name_at))
    for index, (lookup, address, hints, name_at) in enumerate(meta):
        for slot, hint in enumerate(hints):
            struct.pack_into("<Q", idata, lookup + slot * 8, 0x2000 + hint)
            struct.pack_into("<Q", idata, address + slot * 8, 0x2000 + hint)
        struct.pack_into(
            "<IIIII",
            idata,
            index * 20,
            0x2000 + lookup,
            0,
            0,
            0x2000 + name_at,
            0x2000 + address,
        )
    text = b"\xc3" + payload
    file_alignment = 0x200
    text_raw = _align(len(text), file_alignment)
    idata_raw = _align(len(idata), file_alignment)
    text_pointer = 0x400
    idata_pointer = text_pointer + text_raw
    image_size = 0x2000 + _align(len(idata), 0x1000)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<I", optional, 16, 0x1000)
    struct.pack_into("<Q", optional, 24, 0x180000000)
    struct.pack_into("<I", optional, 32, 0x1000)
    struct.pack_into("<I", optional, 36, file_alignment)
    struct.pack_into("<HH", optional, 48, 6, 0)
    struct.pack_into("<I", optional, 56, image_size)
    struct.pack_into("<I", optional, 60, 0x400)
    struct.pack_into("<H", optional, 68, 3)
    struct.pack_into("<I", optional, 108, 16)
    struct.pack_into("<II", optional, 112 + 8, 0x2000, len(idata))
    section_table = bytearray(80)
    struct.pack_into("<8sIIIIIIHHI", section_table, 0, b".text", len(text), 0x1000, text_raw, text_pointer, 0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", section_table, 40, b".idata", len(idata), 0x2000, idata_raw, idata_pointer, 0, 0, 0, 0, 0x40000040)
    coff = struct.pack("<HHIIIHH", 0x8664, 2, 0, 0, 0, len(optional), 0x2022)
    blob = bytearray(idata_pointer + idata_raw)
    blob[0:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    blob[0x84:0x84 + len(coff)] = coff
    blob[0x98:0x98 + len(optional)] = optional
    blob[0x98 + len(optional):0x98 + len(optional) + len(section_table)] = section_table
    blob[text_pointer:text_pointer + len(text)] = text
    blob[idata_pointer:idata_pointer + len(idata)] = idata
    return bytes(blob)


def _good_payload() -> bytes:
    configure = " ".join(pin.LICENSE_TOKENS).encode("ascii")
    return b"\x00" + LICENSE_STRING + b"\x00" + VERSION_STRING + b"\x00" + H264_STRING + b"\x00" + configure + b"\x00"


def _write_headers(legal: Path, configure: str) -> None:
    config_lines = [f"#define {key} {value if key != 'FFMPEG_LICENSE' else chr(34) + value + chr(34)}" for key, value in pin.REQUIRED_CONFIG_H.items()]
    config_lines.append(f'#define FFMPEG_CONFIGURATION "{configure}"')
    (legal / "config.h").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    component_lines = [f"#define {key} {value}" for key, value in pin.REQUIRED_COMPONENTS.items()]
    (legal / "config_components.h").write_text("\n".join(component_lines) + "\n", encoding="utf-8")
    (legal / "config.log").write_text("# ./configure " + configure + "\n", encoding="utf-8")
    (legal / "BUILD.txt").write_text(
        "\n".join([
            f"FFmpeg version {pin.FFMPEG_VERSION}",
            f"source sha256 {pin.FFMPEG_SHA256}",
            "No local patches.",
            "configure: " + configure,
            "--disable-x86asm",
            "",
        ]),
        encoding="utf-8",
    )


def _locate(name: str, digest: str) -> Path:
    candidates = [pin.CACHE_DIR / name, Path("/tmp") / name]
    for candidate in candidates:
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            return candidate
    if name == pin.FFMPEG_TARBALL:
        pin.ensure_file(pin.FFMPEG_URL, pin.CACHE_DIR / name, digest)
    else:
        pin.ensure_file(pin.PYAV_URL, pin.CACHE_DIR / name, digest)
    return pin.CACHE_DIR / name


def _member(tarball: Path, member: str) -> bytes:
    with __import__("tarfile").open(tarball) as archive:
        extracted = archive.extractfile(member)
        if extracted is None:
            raise SystemExit(f"{tarball} is missing {member}")
        return extracted.read()


def self_test() -> None:
    configure = " ".join(pin.LICENSE_TOKENS)
    ffmpeg_tarball = _locate(pin.FFMPEG_TARBALL, pin.FFMPEG_SHA256)
    pyav_tarball = _locate(pin.PYAV_TARBALL, pin.PYAV_SHA256)
    payload = _good_payload()
    dlls = {
        "avcodec-63.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_version_info"]), ("swresample-7.dll", ["swr_alloc"])],
            payload,
        ),
        "avutil-61.dll": _synthetic_dll([("KERNEL32.dll", ["DisableThreadLibraryCalls"])], b"\x00avutil\x00"),
        "swresample-7.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_malloc"])],
            b"\x00swresample\x00",
        ),
        "swscale-10.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_malloc"])],
            b"\x00swscale\x00",
        ),
        "avformat-63.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_malloc"]), ("avcodec-63.dll", ["avcodec_version"])],
            b"\x00avformat\x00",
        ),
        "avdevice-63.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_malloc"])],
            b"\x00avdevice\x00",
        ),
        "avfilter-12.dll": _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_malloc"])],
            b"\x00avfilter\x00",
        ),
        "python313.dll": _synthetic_dll([("KERNEL32.dll", ["DisableThreadLibraryCalls"])], b"\x00python\x00"),
    }
    pyd = _synthetic_dll(
        [
            ("python313.dll", ["PyModule_Create2"]),
            ("avcodec-63.dll", ["avcodec_find_decoder"]),
            ("avutil-61.dll", ["av_frame_alloc"]),
            ("swscale-10.dll", ["sws_getContext"]),
        ],
        b"\x00pyav\x00",
    )
    round_trip = pe_import_dlls(dlls["avcodec-63.dll"])
    if [item.lower() for item in round_trip] != ["kernel32.dll", "avutil-61.dll", "swresample-7.dll"]:
        raise SystemExit(f"synthetic import parser returned {round_trip}")
    with tempfile.TemporaryDirectory(prefix="codec-gate-") as directory:
        bundle = Path(directory)
        runtime = bundle / "runtime"
        packages = runtime / "site-packages" / "av" / "codec"
        packages.mkdir(parents=True)
        for name, blob in dlls.items():
            (runtime / name).write_bytes(blob)
        (packages / "context.pyd").write_bytes(pyd)
        (bundle / "NOTICE.txt").write_text(pin.NOTICE_TEXT, encoding="utf-8")
        legal = bundle / "third-party" / "ffmpeg"
        pyav_legal = bundle / "third-party" / "pyav"
        legal.mkdir(parents=True)
        pyav_legal.mkdir(parents=True)
        _write_headers(legal, configure)
        (legal / "COPYING.LGPLv2.1").write_bytes(_member(ffmpeg_tarball, f"ffmpeg-{pin.FFMPEG_VERSION}/COPYING.LGPLv2.1"))
        (legal / "LICENSE.md").write_bytes(_member(ffmpeg_tarball, f"ffmpeg-{pin.FFMPEG_VERSION}/LICENSE.md"))
        (legal / pin.FFMPEG_TARBALL).write_bytes(ffmpeg_tarball.read_bytes())
        (pyav_legal / pin.PYAV_TARBALL).write_bytes(pyav_tarball.read_bytes())
        (pyav_legal / "LICENSE.txt").write_bytes(_member(pyav_tarball, f"av-{pin.PYAV_VERSION}/LICENSE.txt"))
        (pyav_legal / "BUILD.txt").write_text(
            f"PyAV {pin.PYAV_VERSION}\nsource sha256 {pin.PYAV_SHA256}\nDynamically linked\nNot the PyPI wheel\n",
            encoding="utf-8",
        )
        errors = verify_bundle(bundle)
        if errors:
            raise SystemExit("self-test positive bundle failed:\n" + "\n".join(errors))
        hashed = runtime / "avcodec-63-0123456789abcdef.dll"
        (runtime / "avcodec-63.dll").replace(hashed)
        hashed_errors = verify_bundle(bundle)
        if not any("banned file" in item and hashed.name in item for item in hashed_errors):
            raise SystemExit("self-test did not reject a hashed avcodec DLL name:\n" + "\n".join(hashed_errors))
        hashed.replace(runtime / "avcodec-63.dll")
        bad_payload = payload.replace(b"--disable-gpl", b"--disable-gpl --enable-gpl")
        (runtime / "avcodec-63.dll").write_bytes(_synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_version_info"])],
            bad_payload,
        ))
        if not any("--enable-gpl" in item for item in verify_bundle(bundle)):
            raise SystemExit("self-test did not reject --enable-gpl")
        (runtime / "avcodec-63.dll").write_bytes(dlls["avcodec-63.dll"])
        linked = _synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("libx264-164.dll", ["x264_encoder_open"])],
            payload,
        )
        (runtime / "avcodec-63.dll").write_bytes(linked)
        (runtime / "libx264-164.dll").write_bytes(dlls["avutil-61.dll"])
        problems = verify_bundle(bundle)
        if not any("libx264" in item for item in problems):
            raise SystemExit("self-test did not reject libx264")
        (runtime / "libx264-164.dll").unlink()
        (runtime / "avcodec-63.dll").write_bytes(dlls["avcodec-63.dll"])
        stripped = payload.replace(H264_STRING, b"mpeg4 decoder")
        (runtime / "avcodec-63.dll").write_bytes(_synthetic_dll(
            [("KERNEL32.dll", ["DisableThreadLibraryCalls"]), ("avutil-61.dll", ["av_version_info"]), ("swresample-7.dll", ["swr_alloc"])],
            stripped,
        ))
        if not any("H.264" in item for item in verify_bundle(bundle)):
            raise SystemExit("self-test did not reject a DLL without the H.264 decoder name")
    real = Path("/tmp/ffmpeg-lgpl-win-x64-test/bin/avcodec-63.dll")
    if real.is_file():
        data = real.read_bytes()
        real_errors = codec_dll_errors(data, real.name)
        imports = [item.lower() for item in pe_import_dlls(data)]
        if "avutil-61.dll" not in imports:
            real_errors.append("real DLL does not import avutil-61.dll")
        if any(is_banned_codec_dll(item) for item in imports):
            real_errors.append("real DLL imports a banned codec")
        if any(item in BANNED_RUNTIME for item in imports):
            real_errors.append("real DLL imports a MinGW runtime")
        if real_errors:
            raise SystemExit("cross-compiled avcodec-63.dll failed:\n" + "\n".join(real_errors))
        print(f"self-test also checked {real}")
    print("self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--reject-wheel", type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
    if args.reject_wheel is not None:
        problems = wheel_problems(args.reject_wheel)
        if not problems:
            raise SystemExit(f"{args.reject_wheel} was not rejected. The checker missed a bad wheel.")
        print(f"rejected {args.reject_wheel} ({len(problems)} problems)")
        for item in problems[:12]:
            print(f"- {item}")
    if args.bundle is not None:
        errors = verify_bundle(args.bundle)
        if errors:
            raise SystemExit("Windows codec license gate failed:\n" + "\n".join(f"- {item}" for item in errors))
        print(f"codec gate passed: {args.bundle}")
    if not args.self_test and args.reject_wheel is None and args.bundle is None:
        parser.error("Pass a bundle directory, --self-test, or --reject-wheel.")


if __name__ == "__main__":
    main()
