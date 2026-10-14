"""Command line: python -m attendance <command> --config config.yaml"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
from pathlib import Path

from . import config as config_module


def setup_logging(cfg) -> None:
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
    sub.add_parser("run", help="chạy camera + chấm công + trang admin")
    p = sub.add_parser("enroll", help="đăng ký ảnh mẫu từ thư mục faces/<mã NV>/")
    p.add_argument("--faces", default="faces")
    p = sub.add_parser("evaluate", help="chọn ngưỡng nhận diện từ ảnh đã gán nhãn")
    p.add_argument("--gallery", default="faces")
    p.add_argument("--test", default=None)
    p = sub.add_parser("report", help="in báo cáo một ngày ra màn hình")
    p.add_argument("--day", default=None, help="YYYY-MM-DD, mặc định hôm nay")
    sub.add_parser("test-chat", help="gửi thử một tin nhắn lên Google Chat")
    args = parser.parse_args()

    cfg = config_module.load(args.config)
    setup_logging(cfg)

    if args.command == "run":
        from .pipeline import run

        run(cfg)
    elif args.command == "enroll":
        from .enroll import enroll

        enroll(cfg, cfg.path(args.faces))
    elif args.command == "evaluate":
        from .evaluate import evaluate

        evaluate(cfg, cfg.path(args.gallery), cfg.path(args.test) if args.test else None)
    elif args.command in ("report", "test-chat"):
        from .gallery import Gallery
        from .notifier import GoogleChat
        from .service import AttendanceService
        from .store import Store

        store = Store(cfg.path(cfg.data_dir) / "attendance.db")
        store.sync_employees(cfg.path(cfg.employees_file))
        service = AttendanceService(cfg, store, GoogleChat(cfg.google_chat.webhook_url, store), Gallery())
        if args.command == "report":
            day = dt.date.fromisoformat(args.day) if args.day else service.now().date()
            print(service.summary_text(service.report(day)))
        else:
            if not cfg.google_chat.webhook_url:
                raise SystemExit("google_chat.webhook_url chưa được cấu hình")
            try:
                service.chat.send_now("✅ Kết nối Google Chat của hệ thống chấm công hoạt động.")
            except Exception as exc:
                raise SystemExit(f"Gửi thất bại: {exc}")
            print("Đã gửi, kiểm tra trong Google Chat.")


if __name__ == "__main__":
    main()
