"""Check which macOS version every binary in a built app needs.

    python packaging/macos_min_version.py dist/FaceAttendance.app 12

Reads the Mach-O load commands itself (no otool needed, works on any OS), so the build fails
when a wheel built for a newer macOS slipped in: numpy, OpenCV and onnxruntime wheels made for
macOS 13/14 use Accelerate symbols that older systems do not have, and the app then dies on
start with "Symbol not found: _cblas_...$NEWLAPACK$ILP64".
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

MH_MAGIC_64, MH_CIGAM_64 = 0xFEEDFACF, 0xCFFAEDFE
MH_MAGIC, MH_CIGAM = 0xFEEDFACE, 0xCEFAEDFE
FAT_MAGIC, FAT_CIGAM = 0xCAFEBABE, 0xBEBAFECA
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
PLATFORM_MACOS = 1


def _version(packed: int) -> str:
    return f"{packed >> 16}.{(packed >> 8) & 0xFF}.{packed & 0xFF}"


def _slice_min_version(data: bytes, offset: int) -> str | None:
    magic = struct.unpack_from(">I", data, offset)[0]
    if magic in (MH_MAGIC_64, MH_MAGIC):
        endian, is64 = ">", magic == MH_MAGIC_64
    elif magic in (MH_CIGAM_64, MH_CIGAM):
        endian, is64 = "<", magic == MH_CIGAM_64
    else:
        return None
    header = endian + ("IIIIIIII" if is64 else "IIIIIII")
    fields = struct.unpack_from(header, data, offset)
    ncmds = fields[4]
    pos = offset + struct.calcsize(header)
    found = None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from(endian + "II", data, pos)
        if cmd == LC_BUILD_VERSION:
            platform, minos = struct.unpack_from(endian + "II", data, pos + 8)
            if platform == PLATFORM_MACOS:
                found = _version(minos)
        elif cmd == LC_VERSION_MIN_MACOSX:
            found = _version(struct.unpack_from(endian + "I", data, pos + 8)[0])
        pos += size
    return found


def min_versions(path: Path) -> list[str]:
    """Minimum macOS versions of each architecture slice in the file; [] if not Mach-O."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8:
        return []
    magic = struct.unpack_from(">I", data)[0]
    if magic == FAT_MAGIC:
        count = struct.unpack_from(">I", data, 4)[0]
        out = []
        for i in range(count):
            offset = struct.unpack_from(">I", data, 8 + i * 20 + 8)[0]
            v = _slice_min_version(data, offset)
            if v:
                out.append(v)
        return out
    v = _slice_min_version(data, 0)
    return [v] if v else []


def main(folder: str, maximum: str) -> int:
    limit = tuple(int(p) for p in maximum.split("."))
    worst: tuple[tuple[int, ...], str, str] | None = None
    bad = []
    for path in Path(folder).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        for v in min_versions(path):
            parts = tuple(int(p) for p in v.split("."))
            if worst is None or parts > worst[0]:
                worst = (parts, v, str(path))
            if parts[: len(limit)] > limit:
                bad.append(f"{v:>8}  {path}")
    if worst:
        print(f"Highest minimum macOS version found: {worst[1]} ({worst[2]})")
    if bad:
        print(f"These need a macOS newer than {maximum}:")
        print("\n".join(sorted(bad)))
        return 1
    print(f"OK: everything runs on macOS {maximum}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "12"))
