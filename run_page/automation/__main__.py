"""Command line entry point, also used by the one-shot Docker container."""

from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from .common import Busy, Config, job_lock, read_json, run, utcnow, write_json
from .garmin import authentication_status
from .pipeline import (
    bootstrap,
    make_backup,
    prepare,
    publish,
    push_backup,
    restore_archive,
    synchronize,
)


class RedactedFormatter(logging.Formatter):
    def format(self, record):
        result = super().format(record)
        for key, value in os.environ.items():
            if (
                any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
                and len(value) > 4
            ):
                result = result.replace(value, "<REDACTED>")
        return result


def configure_logging(config):
    directory = config.state / "logs"
    directory.mkdir(exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for path in directory.glob("runner.log.*"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
            path.unlink()
    file_handler = RotatingFileHandler(
        directory / "runner.log", maxBytes=10 * 1024 * 1024, backupCount=9
    )
    # Rotate on a new day as well as on size, so an active small log cannot
    # retain old lines indefinitely just because it never reaches 10 MiB.
    with job_lock(config, "log-rotation.lock", blocking=True):
        active = directory / "runner.log"
        if (
            active.stat().st_size
            and datetime.fromtimestamp(active.stat().st_mtime).date()
            < datetime.now().date()
        ):
            file_handler.doRollover()
        for path in directory.glob("runner.log.*"):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                path.unlink()
    handlers = [logging.StreamHandler(), file_handler]
    formatter = RedactedFormatter("%(asctime)s %(levelname)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Standalone Running Page VPS automation"
    )
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--from", dest="source", required=True)
    sub.add_parser("status")
    sub.add_parser("update")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--all", action="store_true", required=True)
    sub.add_parser("build")
    deploy = sub.add_parser("publish")
    deploy.add_argument("--dry-run", action="store_true")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--to", required=True, dest="release")
    backup = sub.add_parser("backup")
    backup.add_argument("--local-only", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("--archive")
    restore.add_argument("--backup-ref")
    restore.add_argument("--to", required=True)
    args = parser.parse_args(argv)
    config = Config(args.config)
    configure_logging(config)
    if args.command == "status":
        result = {
            key: read_json(config.state / f"{key}.json", {})
            for key in (
                "jobs",
                "sync",
                "render",
                "candidate",
                "deployed",
                "backup",
                "backup-pending",
            )
        }
        result["garmin_auth"] = authentication_status(config)
        result["credentials"] = {
            "garmin": result["garmin_auth"]["method"] != "not_configured",
            "cloudflare": bool(
                os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ACCOUNT_ID")
            ),
            "backup_key": Path("/config/github_ed25519").exists(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    jobs = read_json(config.state / "jobs.json", {})
    started = utcnow()
    backup_guard = None

    def save_job(value):
        with job_lock(config, "status.lock", blocking=True):
            latest = read_json(config.state / "jobs.json", {})
            latest[args.command] = value
            write_json(config.state / "jobs.json", latest)

    try:
        if args.command == "backup":
            backup_guard = (config.state / "backup.lock").open("a")
            fcntl.flock(backup_guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with job_lock(config):
            # A killed container cannot run finally blocks. These are private
            # staging directories used only while this shared lock is held.
            import shutil

            for leftover in config.state.iterdir():
                if leftover.is_dir() and re.fullmatch(
                    r"(?:backup|verify|restore-git)-[a-z0-9_]{8}", leftover.name
                ):
                    shutil.rmtree(leftover)
            (config.state / "latest-backup.tar.gz.tmp").unlink(missing_ok=True)
            if (config.state / "current").is_symlink():
                current = config.current
                for leftover in current.parent.iterdir():
                    if leftover.is_dir() and leftover != current:
                        shutil.rmtree(leftover)
            if args.command == "bootstrap":
                result = bootstrap(config, args.source)
            elif args.command in ("update", "refresh"):
                # Publish retries do not depend on today's Garmin availability
                # or on whether a subsequent build succeeds.
                if read_json(config.state / "candidate.json"):
                    try:
                        publish(config)
                    except Exception:
                        logging.warning("Previous complete deployment is still pending")
                try:
                    result = synchronize(config, full=args.command == "refresh")
                except Exception:
                    # Retry a previously complete candidate, without promoting any
                    # part of this failed synchronization round.
                    if read_json(config.state / "candidate.json"):
                        try:
                            publish(config)
                        except Exception:
                            logging.warning(
                                "Previous complete deployment is still pending"
                            )
                    raise
                prepare(config)
                result["publish"] = publish(config)
            elif args.command == "build":
                result = prepare(config) or {"skipped": "no visible changes"}
            elif args.command == "publish":
                result = publish(config, args.dry_run)
            elif args.command == "rollback":
                if not re.fullmatch(r"[0-9a-f]{64}", args.release):
                    raise RuntimeError(
                        "Rollback requires a complete published input hash"
                    )
                previous = read_json(
                    config.state / "releases" / args.release / "published.json"
                )
                if not previous or previous.get("input_hash") != args.release:
                    raise RuntimeError(
                        "Rollback target is not a retained successful release"
                    )
                write_json(config.state / "candidate.json", previous)
                result = publish(config)
            elif args.command == "backup":
                result = make_backup(config, args.local_only)
            elif args.command == "restore":
                if (
                    Path(args.to)
                    .resolve()
                    .is_relative_to((config.state / "snapshots").resolve())
                ):
                    raise RuntimeError("Restore outside the live snapshots directory")
                archive = args.archive
                if args.backup_ref:
                    import tempfile
                    import shutil

                    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.backup_ref):
                        raise RuntimeError(
                            "--backup-ref must be a complete 40-character commit ID"
                        )
                    scratch = Path(
                        tempfile.mkdtemp(prefix="restore-git-", dir=config.state)
                    )
                    try:
                        run(["git", "init", scratch])
                        run(
                            [
                                "git",
                                "fetch",
                                "--depth=1",
                                config.repository,
                                args.backup_ref,
                            ],
                            cwd=scratch,
                            env={"GIT_TERMINAL_PROMPT": "0"},
                            timeout=120,
                        )
                        archive = scratch / "snapshot.tar"
                        run(
                            [
                                "git",
                                "archive",
                                "--format=tar",
                                f"--output={archive}",
                                "FETCH_HEAD",
                            ],
                            cwd=scratch,
                        )
                        result = restore_archive(archive, args.to)
                    finally:
                        shutil.rmtree(scratch)
                else:
                    result = restore_archive(
                        archive or config.state / "latest-backup.tar.gz", args.to
                    )
        # Network backup push runs outside the data snapshot lock.
        if args.command == "backup" and not args.local_only:
            result = push_backup(config)
        jobs[args.command] = {
            "started_at": started,
            "last_success": utcnow(),
            "consecutive_failures": 0,
            "result": result,
        }
        save_job(jobs[args.command])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Busy, BlockingIOError):
        logging.info("Another operation holds the lock; skipping overlapping run")
        print(json.dumps({"skipped": "another operation is running"}))
        return 0
    except Exception as exc:
        # An exception message is intentionally not printed: HTTP clients and
        # third party auth libraries sometimes include credentials in errors.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        logging.error("%s failed: %s", args.command, message)
        previous = jobs.get(args.command, {})
        jobs[args.command] = {
            **previous,
            "last_attempt": started,
            "error": message,
            "consecutive_failures": previous.get("consecutive_failures", 0) + 1,
        }
        save_job(jobs[args.command])
        return 1
    finally:
        if backup_guard:
            backup_guard.close()


if __name__ == "__main__":
    sys.exit(main())
