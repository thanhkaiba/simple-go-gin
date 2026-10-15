"""Command line: python -m attendance <command> --config config.yaml"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import sys
from pathlib import Path

from . import config as config_module


def setup_logging(cfg) -> None:
    # A Windows service writes the console log with the legacy code page; never fail on emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    log_dir = cfg.path(cfg.data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(log_dir / "attendance.log", maxBytes=5_000_000, backupCount=5,
                                             encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m attendance")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run camera processing, notifications and the admin page")
    p = sub.add_parser("download-models", help="download the model files for a backend")
    p.add_argument("--backend", choices=["insightface", "opencv"], default=None,
                   help="default: the backend in config.yaml")
    p.add_argument("--person", choices=["yolox_tiny", "yolox_s"], nargs="?", const="yolox_tiny", default=None,
                   help="download a person detector for tracking.mode: person instead (default yolox_tiny)")
    p = sub.add_parser("enroll", help="enroll reference photos from faces/<employee id>/")
    p.add_argument("--faces", default="faces")
    p = sub.add_parser("evaluate", help="compare backends and choose thresholds from labelled photos")
    p.add_argument("--gallery", default="faces")
    p.add_argument("--test", default=None)
    p.add_argument("--backends", nargs="+", choices=["insightface", "opencv"], default=None,
                   help="default: the backend in config.yaml")
    p.add_argument("--from-data", action="store_true",
                   help="test on the camera's own snapshots, labelled by admin corrections, instead of folders")
    p.add_argument("--days", type=int, default=30, help="with --from-data: how many days back (default 30)")
    p = sub.add_parser("benchmark", help="run a video through both tracking modes; compare speed and results")
    p.add_argument("video")
    p.add_argument("--modes", nargs="+", choices=["face", "person"], default=["face", "person"])
    p = sub.add_parser("report", help="print the summary for a day")
    p.add_argument("--day", default=None, help="YYYY-MM-DD, default today")
    sub.add_parser("test-chat", help="send a test message to Google Chat")
    args = parser.parse_args()

    cfg = config_module.load(args.config)
    setup_logging(cfg)

    if args.command == "run":
        from .pipeline import run

        run(cfg)
    elif args.command == "download-models":
        from .models import download, download_person

        if args.person:
            download_person(args.person, cfg.path("models"))
        else:
            download(args.backend or cfg.recognition.backend, cfg.path("models"))
    elif args.command == "enroll":
        from .enroll import enroll

        enroll(cfg, cfg.path(args.faces))
    elif args.command == "evaluate":
        if args.from_data:
            from .evaluate import evaluate_data
            from .pipeline import App

            evaluate_data(App(cfg), args.days)
        else:
            from .evaluate import evaluate

            evaluate(cfg, cfg.path(args.gallery), cfg.path(args.test) if args.test else None,
                     args.backends or [cfg.recognition.backend])
    elif args.command == "benchmark":
        from .benchmark import benchmark
        from .pipeline import App

        benchmark(App(cfg), Path(args.video), args.modes)
    elif args.command in ("report", "test-chat"):
        from .pipeline import App

        app = App(cfg)
        if args.command == "report":
            day = dt.date.fromisoformat(args.day) if args.day else app.service.now().date()
            print(app.service.summary_text(app.service.report(day)))
        else:
            if not cfg.google_chat.webhook_url:
                raise SystemExit("google_chat.webhook_url is not set")
            try:
                app.chat.send_now("✅ The attendance system can post to this space.")
            except Exception as exc:
                raise SystemExit(f"Sending failed: {exc}")
            print("Sent, check Google Chat.")


if __name__ == "__main__":
    main()
