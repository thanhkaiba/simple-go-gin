import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
import macos_min_version as mmv  # noqa: E402

from attendance import launcher  # noqa: E402


def mach_o(minos: tuple[int, int], via_build_version: bool = True, magic: int = mmv.MH_MAGIC_64) -> bytes:
    """A minimal 64-bit Mach-O header with one load command naming the minimum macOS version."""
    packed = (minos[0] << 16) | (minos[1] << 8)
    if via_build_version:
        cmd = struct.pack("<IIIIII", mmv.LC_BUILD_VERSION, 24, mmv.PLATFORM_MACOS, packed, packed, 0)
    else:
        cmd = struct.pack("<IIII", mmv.LC_VERSION_MIN_MACOSX, 16, packed, packed)
    header = struct.pack("<IIIIIIII", magic, 0x01000007, 3, 2, 1, len(cmd), 0, 0)
    return header + cmd


def fat(slices: list[bytes]) -> bytes:
    header = struct.pack(">II", mmv.FAT_MAGIC, len(slices))
    offset = 8 + 20 * len(slices)
    table, body = b"", b""
    for s in slices:
        table += struct.pack(">IIIII", 0x01000007, 3, offset + len(body), len(s), 0)
        body += s
    return header + table + body


def test_macos_minimum_version_check(tmp_path, capsys):
    app = tmp_path / "App"
    (app / "lib").mkdir(parents=True)
    (app / "lib" / "ok.so").write_bytes(mach_o((11, 0)))
    (app / "lib" / "old_style.so").write_bytes(mach_o((10, 13), via_build_version=False))
    (app / "lib" / "both.dylib").write_bytes(fat([mach_o((12, 0)), mach_o((11, 0))]))
    (app / "notes.txt").write_text("not a binary")
    assert mmv.main(str(app), "12") == 0
    assert "Highest minimum macOS version found: 12.0.0" in capsys.readouterr().out

    (app / "lib" / "new.so").write_bytes(mach_o((14, 0)))
    assert mmv.main(str(app), "12") == 1
    out = capsys.readouterr().out
    assert "14.0.0" in out and "new.so" in out and "ok.so" not in out.split("newer than")[1]


def test_translocated_app_is_recognised(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/private/var/folders/x/AppTranslocation/ABC/d/FaceAttendance.app/Contents/MacOS/FaceAttendance")
    assert launcher.translocated()
    monkeypatch.setattr(sys, "executable", "/Applications/FaceAttendance.app/Contents/MacOS/FaceAttendance")
    assert not launcher.translocated()


@pytest.mark.skipif(sys.platform == "win32", reason="pid liveness is not checked on Windows")
def test_stale_pid_file_is_removed_but_a_live_one_kept(tmp_path):
    (tmp_path / "running.pid").write_text("999999 999998")
    launcher.Instance(tmp_path).release()
    assert not (tmp_path / "running.pid").exists()
    (tmp_path / "running.pid").write_text(str(os.getpid()))
    launcher.Instance(tmp_path).release()
    assert (tmp_path / "running.pid").exists()


def test_page_warns_when_run_from_a_translocated_copy(core, monkeypatch):
    from test_web import client

    app, _, _ = core
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/private/var/folders/x/AppTranslocation/ABC/FaceAttendance")
    assert "drag <b>FaceAttendance</b> into <b>Applications</b>" in client(app).get("/settings").get_data(as_text=True)
