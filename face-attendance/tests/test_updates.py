import io
import sys
import zipfile
from pathlib import Path

import pytest

from attendance import __version__, config, launcher, models, pipeline, updater


def make_package(path: Path) -> None:
    """A release archive for this platform holding a fake program."""
    exe = updater.exe_in(Path("."))
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(exe.as_posix(), b"program")
    else:
        import tarfile

        with tarfile.open(path, "w:gz") as t:
            info = tarfile.TarInfo(exe.as_posix())
            info.size, info.mode = 7, 0o755
            t.addfile(info, io.BytesIO(b"program"))


def test_versions_compare_as_numbers():
    assert updater.parse("1.10.0") > updater.parse("1.9.3")
    assert updater.parse("v2.0") > updater.parse("1.99")
    assert updater.parse("nonsense") < updater.parse("0.1")


def test_update_is_unpacked_and_handed_over_to(tmp_path, monkeypatch):
    archive = tmp_path / updater.asset_name()
    make_package(archive)
    newer = f"{updater.parse(__version__)[0] + 1}.0.0"
    exe = updater.unpack(archive.as_uri(), newer, tmp_path / "home")
    assert exe.exists() and exe == updater.exe_in(tmp_path / "home" / "versions" / newer)
    assert not list((tmp_path / "home" / "versions").glob("*.part"))
    assert updater.newer_installed(tmp_path / "home") == exe
    # The new version itself does not hand over to itself.
    monkeypatch.setattr(sys, "executable", str(exe))
    assert updater.newer_installed(tmp_path / "home") is None


def test_older_versions_are_not_used_and_cleaned_up(tmp_path):
    home = tmp_path / "home"
    archive = tmp_path / updater.asset_name()
    make_package(archive)
    for v in ("0.1.0", "0.2.0", "0.3.0"):
        updater.unpack(archive.as_uri(), v, home)
    assert updater.newer_installed(home) is None  # all older than this version
    updater.remove_old(home, Path(sys.executable))
    assert [v for v, _ in updater.installed(home)] == ["0.3.0", "0.2.0"]


def write_config(tmp_path, schedule="") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("timezone: Asia/Ho_Chi_Minh\n" + schedule, encoding="utf-8")
    return path


def test_updater_downloads_a_newer_release_and_waits_for_camera_hours_to_end(tmp_path, monkeypatch):
    archive = tmp_path / updater.asset_name()
    make_package(archive)
    newer = f"{updater.parse(__version__)[0] + 1}.0.0"
    monkeypatch.setattr(updater, "latest_release", lambda repo: (newer, archive.as_uri()))
    cfg = write_config(tmp_path, 'schedule:\n  window_start: "00:00"\n  late_after: "00:01"\n  window_end: "23:59"\n'
                                 "  work_days: [0, 1, 2, 3, 4, 5, 6]\n")
    up = updater.Updater(tmp_path, cfg)
    up.check()
    assert up.waiting is not None and up.ready is None
    status = updater.read_status(up.data)
    assert status["latest"] == newer and "ready" in status["state"]
    assert updater.camera_hours(updater.load_settings(cfg))  # so the loop would not restart yet
    up.waiting = None


def test_updater_records_that_it_is_up_to_date_or_could_not_check(tmp_path, monkeypatch):
    up = updater.Updater(tmp_path, write_config(tmp_path))
    monkeypatch.setattr(updater, "latest_release", lambda repo: (__version__, "unused"))
    up.check()
    assert up.waiting is None and updater.read_status(up.data)["latest"] == __version__

    def offline(repo):
        raise OSError("no internet")
    monkeypatch.setattr(updater, "latest_release", offline)
    up.check()
    assert "no internet" in updater.read_status(up.data)["error"]


def test_update_now_button_and_version_on_settings_page(core):
    from attendance.web import create_app

    app, _, _ = core
    c = create_app(app).test_client()
    page = c.get("/settings").get_data(as_text=True)
    assert f"Version {__version__}" in page
    c.post("/settings/update-now")
    assert (app.data_dir / updater.REQUEST_FILE).exists()


def test_missing_models_are_downloaded_with_progress(tmp_path, monkeypatch):
    cfg = config.load(write_config(tmp_path))
    det, rec = (cfg.path(f) for f in cfg.recognition.model_files())
    assert models.missing(cfg) == [det, rec]

    def fake_download(backend, models_dir, progress=None):
        for done in (100, 200):
            progress(done, 200)
        models_dir.mkdir(parents=True, exist_ok=True)
        det.write_bytes(b"x")
        rec.write_bytes(b"x")
    monkeypatch.setattr(models, "download", fake_download)

    seen = []

    class FakeLive:
        def __setattr__(self, name, value):
            seen.append(value)
            object.__setattr__(self, name, value)

    class FakeApp:
        pass

    a = FakeApp()
    a.cfg, a.live = cfg, FakeLive()
    pipeline.fetch_models(a)
    assert models.missing(cfg) == []
    assert any(s and "100%" in s for s in seen) and seen[-1] is None


def test_first_start_writes_a_config_that_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "answering", lambda url: url.endswith(":8000"))  # port 8000 taken
    path = launcher.write_config(tmp_path / "home")
    cfg = config.load(path)
    assert cfg.web.port == 8001 and cfg.web.public_url.endswith(":8001")
    assert cfg.tz  # a time zone Python knows
    assert launcher.write_config(tmp_path / "home") == path  # never overwritten


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses the registry")
def test_autostart_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    launcher.enable_autostart()
    assert "--background" in launcher.autostart_path().read_text()
    launcher.disable_autostart()
    assert not launcher.autostart_path().exists()
