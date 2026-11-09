"""Crash-recoverable snapshot, build, deployment and backup operations."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
import re
import resource
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from .common import (
    atomic_write,
    code_digest,
    content_manifest,
    copy_code,
    copy_data,
    digest_json,
    read_json,
    run,
    utcnow,
    write_json,
)
from .garmin import create_client, init_sources, sync_snapshot


def new_snapshot(config, source, links=True):
    parent = config.state / "snapshots"
    parent.mkdir(exist_ok=True)
    path = parent / uuid.uuid4().hex
    path.mkdir()
    try:
        copy_data(source, path, links=links)
    except BaseException:
        shutil.rmtree(path)
        raise
    return path


def discard_unpublished_snapshot(config, path):
    """Never remove the active data after a post-promotion bookkeeping error."""
    link = config.state / "current"
    if not link.is_symlink() or link.resolve() != Path(path).resolve():
        shutil.rmtree(path, ignore_errors=True)


def validate_snapshot(path):
    path = Path(path)
    with sqlite3.connect(f"file:{path / 'run_page/data.db'}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Snapshot database is corrupt")
        ids = {
            row[0]
            for row in db.execute("SELECT run_id FROM activities WHERE distance > 0.1")
        }
    full = read_json(path / "src/static/activities.json")
    manifest = read_json(path / "src/static/activity_chunks/manifest.json")
    if not isinstance(full, list) or not isinstance(manifest, dict):
        raise RuntimeError("Snapshot JSON/manifest is missing")
    run_ids = [item["run_id"] for item in full]
    if len(run_ids) != len(set(run_ids)) or set(run_ids) != ids:
        raise RuntimeError("Database and JSON activity IDs differ")
    if manifest["total_count"] != len(full):
        raise RuntimeError("Activity manifest count differs")
    seen = []
    for year in manifest["years"]:
        if (
            year["file"] != f"year_{year['year']}.json"
            or not str(year["year"]).isdigit()
        ):
            raise RuntimeError("Invalid activity chunk path")
        chunk = read_json(path / "src/static/activity_chunks" / year["file"])
        expected = [
            item for item in full if item["start_date_local"].startswith(year["year"])
        ]
        if chunk != expected or year["count"] != len(expected):
            raise RuntimeError("Activity chunks and complete JSON differ")
        seen.extend(item["run_id"] for item in chunk)
    if sorted(seen) != sorted(run_ids):
        raise RuntimeError("Incomplete activity chunks")
    return len(full)


def bootstrap(config, source):
    if (config.state / "current").exists():
        raise RuntimeError(
            "Already initialized; bootstrap will not overwrite running data"
        )
    path = new_snapshot(config, source, links=False)
    try:
        validate_snapshot(path)
        init_sources(path / "run_page/data.db", path / "GPX_OUT")
        config.promote(path)
        write_json(
            config.state / "sync.json", {"last_success": utcnow(), "bootstrap": True}
        )
    except BaseException:
        if not (config.state / "current").exists():
            shutil.rmtree(path, ignore_errors=True)
        raise
    return {"activities": validate_snapshot(config.current)}


def synchronize(config, full=False, client=None):
    path = new_snapshot(config, config.current)

    async def execute():
        active = client or create_client(config)
        try:
            return await sync_snapshot(
                path, active, config.days, full, queue_global=config.global_sync
            )
        finally:
            if client is None:
                await active.close()

    try:
        result = asyncio.run(execute())
        validate_snapshot(path)
        config.promote(path)
        write_json(config.state / "sync.json", {"last_success": utcnow(), **result})
        return result
    except BaseException:
        discard_unpublished_snapshot(config, path)
        raise


def render(config, snapshot, date):
    import sys

    env = {
        "RUNNING_PAGE_DATA_ROOT": str(snapshot),
        "TZ": "Asia/Shanghai",
        "RUNNING_PAGE_RENDER_AT": f"{date}T00:00:00",
    }
    base = [
        sys.executable,
        config.source / "run_page/gen_svg.py",
        "--from-db",
        "--use-localtime",
    ]
    common = ["--athlete", config.athlete]
    jobs = [
        [
            "--type",
            "github",
            "--github-style",
            "align-firstday",
            "--title",
            "ClF3 Running",
            "--min-distance",
            "0.5",
            "--output",
            snapshot / "assets/github.svg",
        ],
        [
            "--type",
            "grid",
            "--title",
            "Over 10km Runs",
            "--min-distance",
            "10",
            "--output",
            snapshot / "assets/grid.svg",
        ],
        ["--type", "circular", "--output", snapshot / "assets/year.svg"],
        [
            "--type",
            "github",
            "--year",
            date[:4],
            "--language",
            "zh_CN",
            "--github-style",
            "align-firstday",
            "--title",
            f"{date[:4]} Running",
            "--min-distance",
            "0.5",
            "--output",
            snapshot / f"assets/github_{date[:4]}.svg",
        ],
        ["--type", "year_summary", "--output", snapshot / "assets/year_summary.svg"],
    ]
    for sport in (
        "all",
        "running",
        "walking",
        "hiking",
        "cycling",
        "swimming",
        "skiing",
    ):
        filename = "mol.svg" if sport == "all" else f"mol_{sport}.svg"
        title = {
            "all": "Month of Life",
            "running": "Runner Month of Life",
            "walking": "Walker Month of Life",
            "hiking": "Hiker Month of Life",
            "cycling": "Cyclist Month of Life",
            "swimming": "Swimmer Month of Life",
            "skiing": "Skier Month of Life",
        }[sport]
        jobs.append(
            [
                "--type",
                "monthoflife",
                "--birth",
                config.birth_month,
                "--sport-type",
                sport,
                "--title",
                title,
                "--output",
                snapshot / "assets" / filename,
            ]
        )
    for args in jobs:
        extra = []
        if args[1] in {"github", "grid"}:
            extra = [
                "--special-distance",
                "20" if args[1] == "grid" else "10",
                "--special-distance2",
                "40" if args[1] == "grid" else "20",
                "--special-color",
                "yellow",
                "--special-color2",
                "red",
            ]
        elif args[1] == "monthoflife":
            extra = [
                "--special-distance",
                "10",
                "--special-distance2",
                "20",
                "--special-color",
                "#f9d367",
                "--special-color2",
                "#f0a1a8",
            ]
        run([*base, *common, *args, *extra], env=env, timeout=config.timeout)


def prepare(config):
    date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    snapshot = config.current
    activities = read_json(snapshot / "src/static/activities.json")
    source_hash = code_digest(config.source)
    request_hash = digest_json(
        {
            "data": activities,
            "code": source_hash,
            "date": date,
            "athlete": config.athlete,
            "birth": config.birth_month,
        }
    )
    rendered = read_json(config.state / "render.json", {})
    if rendered.get("request_hash") != request_hash:
        candidate = new_snapshot(config, snapshot)
        try:
            render(config, candidate, date)
            validate_snapshot(candidate)
            config.promote(candidate)
        except BaseException:
            discard_unpublished_snapshot(config, candidate)
            raise
        snapshot = config.current
        write_json(
            config.state / "render.json", {"request_hash": request_hash, "date": date}
        )
    inputs = {
        "code": source_hash,
        "activities": activities,
        "assets": content_manifest(snapshot / "assets"),
        "site_url": config.site_url,
        "worker": config.worker,
    }
    fingerprint = digest_json(inputs)
    deployed = read_json(config.state / "deployed.json", {})
    if deployed.get("input_hash") == fingerprint:
        return None
    previous = read_json(config.state / "candidate.json", {})
    release = config.state / "releases" / fingerprint
    if (
        previous.get("input_hash") == fingerprint
        and (release / "dist/release.json").exists()
        and read_json(release / "files.json") == content_manifest(release / "dist")
    ):
        return previous
    release.mkdir(parents=True, exist_ok=True)
    work = release / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    copy_code(config.source, work)
    # Only public presentation inputs enter the build, never the DB or raw GPS.
    for rel in ("assets", "src/static/activities.json", "src/static/activity_chunks"):
        src, dest = snapshot / rel, work / rel
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    (work / "node_modules").symlink_to(
        config.source / "node_modules", target_is_directory=True
    )
    try:
        started = time.monotonic()
        run(
            ["pnpm", "build"],
            cwd=work,
            env={"PATH_PREFIX": "/", "TZ": "Asia/Shanghai"},
            timeout=config.timeout,
        )
        (work / "dist/404.html").unlink(missing_ok=True)
        if not (work / "dist/index.html").is_file():
            raise RuntimeError("Build did not produce index.html")
        result = {
            "input_hash": fingerprint,
            "code_hash": source_hash,
            "built_at": utcnow(),
            "code_revision": os.getenv("RUNNING_PAGE_SOURCE_REVISION", "uncommitted"),
            "build_seconds": round(time.monotonic() - started, 2),
            "max_child_rss_kib": int(
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                / (1024 if os.uname().sysname == "Darwin" else 1)
            ),
        }
        write_json(work / "dist/release.json", result)
        if (release / "dist").exists():
            shutil.rmtree(release / "dist")
        shutil.move(work / "dist", release / "dist")
        write_json(release / "files.json", content_manifest(release / "dist"))
        write_json(config.state / "candidate.json", result)
        prune_releases(config, release)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def prune_releases(config, pending):
    """Keep two published builds and the latest complete pending candidate."""
    paths = [
        p
        for p in (config.state / "releases").iterdir()
        if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[0-9a-f]{64}", p.name)
    ]
    published = [p for p in paths if (p / "published.json").exists()]
    published.sort(
        key=lambda p: read_json(p / "published.json").get("deployed_at", ""),
        reverse=True,
    )
    keep = {pending, *published[:2]}
    for path in paths:
        if path not in keep:
            shutil.rmtree(path)


def probe(config, candidate):
    try:
        url = config.site_url + "/release.json?version=" + candidate["input_hash"]
        request = urllib.request.Request(
            url,
            headers={
                "Cache-Control": "no-cache",
                "Accept": "application/json",
                "User-Agent": "running-page-deployer/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response).get("input_hash") == candidate["input_hash"]
    except (OSError, ValueError):
        return False


def publish(config, dry_run=False):
    candidate = read_json(config.state / "candidate.json")
    if not candidate:
        return {"skipped": "no pending release"}
    release = config.state / "releases" / candidate["input_hash"]
    if not (release / "dist/release.json").is_file():
        raise RuntimeError("Pending build is missing; prepare it again")
    if read_json(release / "files.json") != content_manifest(release / "dist"):
        raise RuntimeError("Pending build checksums differ; build it again")
    hostname = urllib.parse.urlsplit(config.site_url).hostname
    if hostname not in {"run.clf3.org", "run-dev.clf3.org"}:
        raise RuntimeError("Unsupported running-page deployment hostname")
    deployed = read_json(config.state / "deployed.json", {})
    if deployed.get("input_hash") == candidate["input_hash"]:
        return {"skipped": "already deployed"}
    if not dry_run:
        if not os.getenv("CLOUDFLARE_API_TOKEN") or not os.getenv(
            "CLOUDFLARE_ACCOUNT_ID"
        ):
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required"
            )
        # Validate the configured custom domain before making any deployment change.
        request = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}/workers/domains",
            headers={"Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"]},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            domains = json.load(response)
        if not domains.get("success") or not any(
            x.get("hostname") == hostname
            and x.get("service") == config.worker
            and x.get("environment", "production") == "production"
            and x.get("enabled", True)
            for x in domains.get("result", [])
        ):
            raise RuntimeError(
                "Cloudflare custom domain does not match the configured Worker"
            )
    # Keep custom domains managed in Cloudflare. Setting routes here makes
    # Wrangler replace the Worker's existing custom-domain bindings.
    wrangler = {
        "name": config.worker,
        "compatibility_date": "2026-11-06",
        "workers_dev": True,
        "assets": {
            "directory": "./dist",
            "not_found_handling": "single-page-application",
        },
    }
    write_json(release / "wrangler.json", wrangler)
    args = [
        config.source / "node_modules/.bin/wrangler",
        "deploy",
        "--config",
        release / "wrangler.json",
    ]
    if dry_run:
        run([*args, "--dry-run"], cwd=release, timeout=config.timeout)
        return {"dry_run": True, **candidate}
    if not probe(config, candidate):
        run(args, cwd=release, timeout=config.timeout)
        # Allow transient probe failures and version propagation after upload.
        for delay in (0, 2, 4, 8):
            if delay:
                time.sleep(delay)
            if probe(config, candidate):
                break
        else:
            raise RuntimeError(
                "Upload finished but online version is not yet verified; pending release retained"
            )
    result = {**candidate, "deployed_at": utcnow()}
    write_json(config.state / "deployed.json", result)
    write_json(release / "published.json", result)
    prune_releases(config, release)
    return result


def make_backup(config, local_only=False):
    """Prepare a fully checked snapshot and Git commit; pushing is independent."""
    temporary = Path(tempfile.mkdtemp(prefix="backup-", dir=config.state))
    try:
        copy_code(config.source, temporary)
        copy_data(config.current, temporary)
        validate_snapshot(temporary)
        write_json(
            temporary / "automation-config.json",
            {
                "site_url": config.site_url,
                "worker": config.worker,
                "athlete": config.athlete,
                "birth_month": config.birth_month,
                "refresh_days": config.days,
                "garmin_region": config.garmin_region,
                "global_sync": config.global_sync,
                "global_sync_limit": config.global_sync_limit,
                "repository": config.repository,
                "backup_branch": config.branch,
            },
        )
        manifest = content_manifest(temporary)
        version = digest_json(manifest)
        write_json(
            temporary / "backup-manifest.json",
            {
                "version": 1,
                "content_hash": version,
                "files": manifest,
                "code_hash": code_digest(config.source),
                "code_revision": os.getenv(
                    "RUNNING_PAGE_SOURCE_REVISION", "uncommitted"
                ),
            },
        )
        # Validate the archive by restoring it before replacing the only good copy.
        archive_tmp = config.state / "latest-backup.tar.gz.tmp"
        with tarfile.open(archive_tmp, "w:gz", compresslevel=1) as archive:
            for entry in sorted(temporary.iterdir()):
                archive.add(entry, arcname=entry.name, recursive=True)
        verify = Path(tempfile.mkdtemp(prefix="verify-", dir=config.state))
        try:
            restore_archive(archive_tmp, verify)
        finally:
            shutil.rmtree(verify)
        with archive_tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(archive_tmp, config.state / "latest-backup.tar.gz")
        descriptor = os.open(config.state, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if local_only:
            return {"content_hash": version, "archive": "latest-backup.tar.gz"}
        repository = config.state / "backup-git"
        if not (repository / ".git/automation-ready").exists():
            repository.mkdir(exist_ok=True)
            run(["git", "init", "-b", config.branch, repository])
            run(
                ["git", "remote", "remove", "origin"],
                cwd=repository,
                capture=True,
                check=False,
            )
            run(["git", "remote", "add", "origin", config.repository], cwd=repository)
            # Preserve remote history on fresh recovery. Any transport error aborts,
            # rather than incorrectly treating an inaccessible branch as absent.
            env = {
                "GIT_TERMINAL_PROMPT": "0",
            }
            remote = run(
                ["git", "ls-remote", "--exit-code", "--heads", "origin", config.branch],
                cwd=repository,
                env=env,
                timeout=90,
                capture=True,
                check=False,
            )
            if remote.returncode == 0:
                run(
                    ["git", "fetch", "origin", config.branch],
                    cwd=repository,
                    env=env,
                    timeout=120,
                )
                run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repository)
            elif remote.returncode != 2:
                raise RuntimeError(
                    "Cannot inspect backup branch; local archive is ready, Git backup pending"
                )
            atomic_write(repository / ".git/automation-ready", "ready\n")
        # Generated file list and code allowlist are the only paths eligible for Git.
        for path in list(repository.iterdir()):
            if path.name != ".git":
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for path in temporary.iterdir():
            out = repository / path.name
            shutil.copytree(path, out) if path.is_dir() else shutil.copy2(path, out)
        run(["git", "config", "user.name", "Running Page Backup"], cwd=repository)
        run(["git", "config", "user.email", "backup@clf3.org"], cwd=repository)
        paths = [*manifest, "backup-manifest.json"]
        run(["git", "add", "--all", "--", *paths], cwd=repository)
        # Stage previously tracked removals too, without adding any unlisted files.
        run(["git", "add", "-u"], cwd=repository)
        diff = run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repository,
            check=False,
            capture=True,
        )
        if diff.returncode == 1:
            run(
                ["git", "commit", "-m", f"backup running data {version[:12]}"],
                cwd=repository,
            )
        elif diff.returncode != 0:
            raise RuntimeError("Git backup comparison failed")
        commit = run(
            ["git", "rev-parse", "HEAD"], cwd=repository, capture=True
        ).stdout.strip()
        pending = {"content_hash": version, "commit": commit, "prepared_at": utcnow()}
        write_json(config.state / "backup-pending.json", pending)
        return pending
    finally:
        shutil.rmtree(temporary)
        (config.state / "latest-backup.tar.gz.tmp").unlink(missing_ok=True)


def push_backup(config):
    pending = read_json(config.state / "backup-pending.json")
    if not pending:
        return {"skipped": "no pending backup"}
    env = {
        "GIT_TERMINAL_PROMPT": "0",
    }
    run(
        ["git", "push", "origin", f"HEAD:refs/heads/{config.branch}"],
        cwd=config.state / "backup-git",
        env=env,
        timeout=120,
    )
    result = {**pending, "pushed_at": utcnow()}
    write_json(config.state / "backup.json", result)
    (config.state / "backup-pending.json").unlink()
    return result


def restore_archive(archive_path, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise RuntimeError("Restore target must be empty")
    with tarfile.open(archive_path) as archive:
        for item in archive.getmembers():
            if (
                item.issym()
                or item.islnk()
                or item.isdev()
                or not item.name
                or Path(item.name).is_absolute()
                or ".." in Path(item.name).parts
            ):
                raise RuntimeError("Unsafe backup archive entry")
        archive.extractall(target, filter="data")
    manifest = read_json(target / "backup-manifest.json")
    actual = content_manifest(target)
    actual.pop("backup-manifest.json", None)
    if (
        not manifest
        or manifest["files"] != actual
        or digest_json(actual) != manifest["content_hash"]
    ):
        raise RuntimeError("Backup checksums differ")
    validate_snapshot(target)
    return {
        "content_hash": manifest["content_hash"],
        "activities": validate_snapshot(target),
    }
