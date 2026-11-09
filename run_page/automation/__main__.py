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
from .global_client import (
    LoginError,
    auth_status as global_auth_status,
    login as global_login,
)
from .global_sync import (
    deliver as deliver_global,
    link_activity,
    sync_status as global_sync_status,
)
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
    global_sync = sub.add_parser(
        "global-sync", help="Deliver queued CN activities to the international account"
    )
    global_sync.add_argument(
        "--backfill", action="store_true", help="Include all existing local history"
    )
    global_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Queue and reconcile without destination writes",
    )
    global_sync.add_argument("--limit", type=int)
    global_sync.add_argument("--retry-blocked", action="store_true")
    global_login_parser = sub.add_parser(
        "global-login", help="Interactive international login; no password stored"
    )
    global_login_parser.add_argument(
        "--import-file", help="Session file, or - for stdin"
    )
    global_link = sub.add_parser(
        "global-link", help="Resolve an ambiguous destination activity explicitly"
    )
    global_link.add_argument("--source-id", required=True)
    global_link.add_argument("--target-id", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive")
    restore.add_argument("--backup-ref")
    restore.add_argument("--to", required=True)
    args = parser.parse_args(argv)
    config = Config(args.config)
    if args.command == "global-login":
        try:
            global_login(config, args.import_file)
            return 0
        except LoginError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(
                f"International login failed during setup ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
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
        result["global_auth"] = global_auth_status(config)
        if (config.state / "current").is_symlink():
            result["global_sync"] = global_sync_status(config)
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

    def save_job(value, name=None):
        with job_lock(config, "status.lock", blocking=True):
            latest = read_json(config.state / "jobs.json", {})
            latest[name or args.command] = value
            write_json(config.state / "jobs.json", latest)

    def try_global_sync():
        if not config.global_sync:
            return {"skipped": "international synchronization is disabled"}
        previous = read_json(config.state / "jobs.json", {}).get("global-sync", {})
        try:
            outcome = deliver_global(config, backfill=True)
            healthy = not outcome.get("failed") and not outcome.get("counts", {}).get(
                "blocked"
            )
            job = {
                "last_attempt": utcnow(),
                "result": outcome,
                "consecutive_failures": (
                    0 if healthy else previous.get("consecutive_failures", 0) + 1
                ),
            }
            if healthy:
                job["last_success"] = utcnow()
            elif previous.get("last_success"):
                job["last_success"] = previous["last_success"]
            save_job(job, "global-sync")
            return outcome
        except Exception:
            # International writes never prevent a complete website release.
            save_job(
                {
                    **previous,
                    "last_attempt": utcnow(),
                    "error": "international_delivery_failed",
                    "consecutive_failures": previous.get("consecutive_failures", 0) + 1,
                },
                "global-sync",
            )
            logging.warning("International delivery incomplete; durable queue retained")
            return {"error": "international_delivery_failed"}

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
                    try_global_sync()
                    raise
                try:
                    prepare(config)
                    result["publish"] = publish(config)
                finally:
                    result["global_sync"] = try_global_sync()
            elif args.command == "global-sync":
                if args.limit is not None and not 1 <= args.limit <= 1000:
                    raise RuntimeError(
                        "International batch limit must be between 1 and 1000"
                    )
                result = deliver_global(
                    config,
                    backfill=args.backfill,
                    dry_run=args.dry_run,
                    limit=args.limit,
                    retry_blocked=args.retry_blocked,
                )
                if not args.dry_run and (
                    result.get("failed") or result.get("counts", {}).get("blocked")
                ):
                    raise RuntimeError(
                        "International delivery has incomplete activities; inspect status"
                    )
            elif args.command == "global-link":
                result = link_activity(config, args.source_id, args.target_id)
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
