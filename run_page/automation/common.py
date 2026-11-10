"""Configuration, durable writes, snapshot ownership and bounded subprocesses."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
DATA_PATHS = (
    "GPX_OUT",
    "TCX_OUT",
    "FIT_OUT",
    "ORIGINAL_OUT",
    "run_page/data.db",
    "imported.json",
    "src/static/activities.json",
    "src/static/activity_chunks",
    "assets",
)
EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    ".cache",
    "__pycache__",
    ".wrangler",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
}


class Busy(RuntimeError):
    """An expected skipped run, not a synchronization or deployment failure."""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    atomic_write(
        path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_manifest(root):
    root = Path(root)
    return {
        str(p.relative_to(root)): digest_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def digest_json(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def code_files(root):
    root = Path(root)
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED
            and d
            not in {
                "GPX_OUT",
                "TCX_OUT",
                "FIT_OUT",
                "ORIGINAL_OUT",
                "activities",
                "Workouts",
                "PNG_OUT",
            }
        )
        for name in sorted(files):
            path = Path(current) / name
            rel = path.relative_to(root)
            if (
                name in EXCLUDED
                or path.is_symlink()
                or name.startswith(".env")
                or name == "config.yaml"
                or name.startswith(".dev.vars")
                or str(rel) == "ops/runtime.env"
                or name.startswith("data.db")
                or name == "imported.json"
                or str(rel).startswith("src/static/activity_chunks/")
                or str(rel) == "src/static/activities.json"
                or (rel.parts[0] == "assets" and name.endswith(".svg"))
            ):
                continue
            yield path, rel


def copy_code(source, target):
    target = Path(target)
    for path, rel in code_files(source):
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def code_digest(root):
    return digest_json({str(rel): digest_file(path) for path, rel in code_files(root)})


def backup_database(source, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{Path(source).absolute()}?mode=ro", uri=True) as src:
        with sqlite3.connect(dest) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database integrity check failed")


def copy_data(source, target, links=False):
    source, target = Path(source), Path(target)
    for rel in DATA_PATHS:
        src, dst = source / rel, target / rel
        if not src.exists():
            continue
        if rel == "run_page/data.db":
            backup_database(src, dst)
        elif src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for item in sorted(src.rglob("*")):
                if not item.is_file() or item.is_symlink():
                    continue
                # Only generated SVGs belong in the data snapshot, never TS code.
                if rel == "assets" and item.suffix != ".svg":
                    continue
                out = dst / item.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.exists():
                    out.unlink()
                if links and rel in {"GPX_OUT", "TCX_OUT", "FIT_OUT", "ORIGINAL_OUT"}:
                    try:
                        os.link(item, out)
                        continue
                    except OSError:
                        pass
                shutil.copy2(item, out)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


class Config:
    def __init__(self, path=None, **overrides):
        values = read_json(
            path or os.getenv("RUNNING_PAGE_CONFIG", "/config/config.json"), {}
        )
        values.update(overrides)
        self.source = Path(values.get("source", ROOT)).absolute()
        self.state = Path(
            values.get("state", os.getenv("RUNNING_PAGE_STATE", "/data"))
        ).absolute()
        self.site_url = values.get("site_url", "https://run.clf3.org").rstrip("/")
        self.worker = values.get("worker", "running-page")
        self.repository = values.get(
            "repository", "git@github.com:ClF3/running_page.git"
        )
        self.branch = values.get("backup_branch", "data-backup")
        if urllib.parse.urlsplit(self.repository).password or (
            self.repository.startswith("https://")
            and urllib.parse.urlsplit(self.repository).username
        ):
            raise ValueError("Do not embed credentials in the backup repository URL")
        self.athlete = values.get("athlete", "ClF3")
        self.birth_month = values.get("birth_month", "1989-03")
        self.days = int(values.get("refresh_days", 30))
        self.garmin_region = values.get("garmin_region", "cn")
        self.global_sync = values.get("global_sync", False)
        self.global_sync_standalone = values.get("global_sync_standalone", False)
        self.global_sync_all_activities = values.get(
            "global_sync_all_activities", False
        )
        self.global_sync_limit = int(values.get("global_sync_limit", 25))
        self.timeout = int(values.get("command_timeout", 1800))
        if (
            self.days < 1
            or not isinstance(self.global_sync, bool)
            or not isinstance(self.global_sync_standalone, bool)
            or (self.global_sync_standalone and not self.global_sync)
            or not isinstance(self.global_sync_all_activities, bool)
            or (self.global_sync_all_activities and not self.global_sync)
            or not 1 <= self.global_sync_limit <= 1000
            or (self.global_sync and self.garmin_region != "cn")
            or self.garmin_region not in {"cn", "global"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]*", self.branch)
            or self.branch in {"master", "main"}
            or ".." in self.branch
        ):
            raise ValueError("Invalid refresh window or backup branch")
        self.state.mkdir(parents=True, exist_ok=True)

    @property
    def current(self):
        link = self.state / "current"
        if not link.is_symlink():
            raise RuntimeError(
                "No complete snapshot. Run bootstrap --from <repository> first."
            )
        resolved = link.resolve(strict=True)
        if resolved.parent != (self.state / "snapshots").resolve():
            raise RuntimeError("Invalid current snapshot path")
        return resolved

    def promote(self, directory):
        directory = Path(directory)
        # A successful generation must survive a power loss, not just a Python
        # exception. Flush files before making the new snapshot authoritative.
        for path in directory.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        tmp = self.state / ".next-current"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(directory.relative_to(self.state))
        os.replace(tmp, self.state / "current")
        descriptor = os.open(self.state, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for old in (self.state / "snapshots").iterdir():
            if old != directory and old.is_dir():
                shutil.rmtree(old)


@contextlib.contextmanager
def job_lock(config, name="runner.lock", blocking=False, *, wait_seconds=0):
    deadline = time.monotonic() + wait_seconds
    with (config.state / name).open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Busy(
                        "Another automation operation is running; retry later"
                    ) from exc
                time.sleep(min(1, remaining))
        yield


def run(args, cwd=None, env=None, timeout=1800, capture=False, check=True):
    logging.info("Running %s", Path(str(args[0])).name)
    result = subprocess.run(
        [str(x) for x in args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        timeout=timeout,
        text=True,
        capture_output=True,
    )
    if result.returncode and check:
        for line in result.stderr.splitlines()[-20:]:
            logging.error("%s", line)
        # Commands may contain tokens in errors. Never echo raw command/output.
        raise RuntimeError(
            f"{Path(str(args[0])).name} failed (exit {result.returncode})"
        )
    if not capture:
        for line in result.stdout.splitlines()[-8:]:
            logging.info("%s", line)
    return result
