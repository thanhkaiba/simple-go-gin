"""Automatic updates for the packaged program.

New versions are published as GitHub releases (tag v1.2.3) with one archive per platform, e.g.
FaceAttendance-windows-x64.zip. The launcher checks for a newer release now and then, unpacks it
into <data folder>/versions/<version>/ and, outside camera hours, restarts into it. The installed
program stays where the installer put it and hands over to the newest unpacked version when it
starts, so updating never needs to write to the install folder or ask for permission.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

REPO = "thanhkaiba/simple-go-gin"
STATUS_FILE = "update.json"     # in the data folder; shown on the Settings page
REQUEST_FILE = "update.request"  # written by the Settings page: check now
KEEP_VERSIONS = 2


def parse(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.lstrip("v").split("-")[0].split("."))
    except ValueError:
        return (0,)


def platform_name() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def asset_name() -> str:
    return f"FaceAttendance-{platform_name()}" + (".zip" if sys.platform == "win32" else ".tar.gz")


def exe_in(folder: Path) -> Path:
    if sys.platform == "darwin":
        return folder / "FaceAttendance.app" / "Contents" / "MacOS" / "FaceAttendance"
    return folder / "FaceAttendance" / ("FaceAttendance.exe" if sys.platform == "win32" else "FaceAttendance")


def latest_release(repo: str = REPO) -> tuple[str, str] | None:
    """(version, download URL) of the newest release that has a package for this computer."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "FaceAttendance"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # no release published yet
            return None
        raise
    for asset in release.get("assets", []):
        if asset["name"] == asset_name():
            return release["tag_name"].lstrip("v"), asset["browser_download_url"]
    return None


def installed(home: Path) -> list[tuple[str, Path]]:
    """Unpacked versions, newest first, as (version, program)."""
    found = []
    for folder in (home / "versions").glob("*"):
        if folder.is_dir() and not folder.name.endswith(".part") and exe_in(folder).exists():
            found.append((folder.name, exe_in(folder)))
    return sorted(found, key=lambda v: parse(v[0]), reverse=True)


def newer_installed(home: Path) -> Path | None:
    """The program to hand over to, if an unpacked version is newer than this one."""
    versions = installed(home)
    if versions and parse(versions[0][0]) > parse(__version__) and versions[0][1] != Path(sys.executable):
        return versions[0][1]
    return None


def unpack(url: str, version: str, home: Path) -> Path:
    versions = home / "versions"
    target, partial = versions / version, versions / (version + ".part")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive = versions / (version + "-" + url.rsplit("/", 1)[1])
    with urllib.request.urlopen(url, timeout=120) as resp, open(archive, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(partial)
        else:
            with tarfile.open(archive) as t:  # keeps the program's permissions and the macOS app's links
                t.extractall(partial, filter="tar") if hasattr(tarfile, "data_filter") else t.extractall(partial)
    finally:
        archive.unlink(missing_ok=True)
    if not exe_in(partial).exists():
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(f"{url} does not contain {exe_in(Path('.'))}")
    shutil.rmtree(target, ignore_errors=True)
    partial.rename(target)
    return exe_in(target)


def remove_old(home: Path, running: Path) -> None:
    for version, exe in installed(home)[KEEP_VERSIONS:]:
        if exe != running:
            shutil.rmtree(home / "versions" / version, ignore_errors=True)


def write_status(data: Path, **fields) -> None:
    path = data / STATUS_FILE
    status = read_status(data)
    status.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status), encoding="utf-8")


def read_status(data: Path) -> dict:
    try:
        return json.loads((data / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_settings(config: Path):
    """config.yaml with the values saved on the Settings page, read fresh each time."""
    from . import config as config_module, settings
    from .store import Store

    cfg = config_module.load(config)
    store = Store(cfg.path(cfg.data_dir) / "attendance.db")
    try:
        settings.load(cfg, store)
    finally:
        store.close()
    return cfg


def camera_hours(cfg) -> bool:
    """True during the camera window on a work day: no restarts then."""
    now = dt.datetime.now(cfg.tz)
    start, _, end = cfg.schedule.clocks()
    return now.weekday() in cfg.schedule.work_days and start <= now.time() < end


class Updater:
    """Runs in the launcher, next to the attendance program. `ready` is set to the new program once
    it is unpacked and it is a good moment to restart."""

    def __init__(self, home: Path, config: Path, repo: str = REPO):
        self.home, self.config, self.repo = home, config, repo
        cfg = load_settings(config)
        self.data = cfg.path(cfg.data_dir)
        write_status(self.data, state="")  # a restart after an update starts here
        self.ready: Path | None = None
        self.waiting: Path | None = None  # unpacked, waiting for the camera hours to end

    def check(self) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        try:
            found = latest_release(self.repo)
        except Exception as exc:
            write_status(self.data, checked_at=now, error=f"Could not check for updates: {exc}")
            return
        if not found or parse(found[0]) <= parse(__version__):
            write_status(self.data, checked_at=now, latest=found[0] if found else __version__, error="", state="")
            return
        version, url = found
        log.info("Downloading version %s", version)
        write_status(self.data, checked_at=now, latest=version, error="", state=f"Downloading version {version}")
        try:
            self.waiting = unpack(url, version, self.home)
        except Exception as exc:
            write_status(self.data, error=f"Could not download version {version}: {exc}", state="")
            return
        write_status(self.data, state=f"Version {version} is ready; it starts after camera hours")

    def loop(self, first_check_s: float = 120, poll_s: float = 30) -> None:
        next_check = time.time() + first_check_s
        request = self.data / REQUEST_FILE
        while self.ready is None:
            time.sleep(poll_s)
            try:
                cfg = load_settings(self.config)
                asked = request.exists()
                if asked:
                    request.unlink(missing_ok=True)
                if self.waiting is None and (asked or (cfg.updates.enabled and time.time() >= next_check)):
                    next_check = time.time() + max(cfg.updates.check_hours, 0.25) * 3600
                    self.check()
                if self.waiting is not None and (asked or not camera_hours(cfg)):
                    self.ready = self.waiting
            except Exception:
                log.exception("Update check failed")
