"""Validate a reviewed code version before atomically selecting its directory."""

import argparse
import fcntl
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, timeout=3600, **kwargs)


def active(unit):
    return subprocess.run([*manager(), "is-active", "--quiet", unit]).returncode == 0


def manager():
    return ["systemctl", "--user"] if os.getenv("RUNNING_PAGE_ROOT") else ["systemctl"]


def checkout(target, revision):
    if target.exists():
        found = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=target, text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=target, text=True
        ).strip()
        if found != revision or dirty:
            raise RuntimeError(
                "Existing version must be a clean checkout of the requested commit"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # A failed fetch must not leave a half-checkout that blocks the next retry.
    with tempfile.TemporaryDirectory(prefix=".checkout-", dir=target.parent) as tmp:
        checkout_new(Path(tmp), revision)
        os.replace(tmp, target)


def checkout_new(target, revision):
    run(["git", "init", target])
    run(
        [
            "git",
            "fetch",
            "--filter=blob:none",
            "--depth=1",
            "https://github.com/ClF3/running_page.git",
            revision,
        ],
        cwd=target,
    )
    patterns = [
        "/*",
        "!/GPX_OUT/",
        "!/TCX_OUT/",
        "!/FIT_OUT/",
        "!/activities/",
        "!/Workouts/",
        "!/run_page/data.db",
        "!/imported.json",
        "!/src/static/activities.json",
        "!/src/static/activity_chunks/",
        "!/assets/*.svg",
    ]
    run(["git", "sparse-checkout", "set", "--no-cone", *patterns], cwd=target)
    run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ref", required=True, help="Complete reviewed code commit SHA"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.ref):
        parser.error("--ref must be a full 40-character commit SHA")
    user_root = os.getenv("RUNNING_PAGE_ROOT")
    if not user_root and os.geteuid() != 0:
        parser.error("Run via sudo ops/run upgrade")
    code_root = Path(user_root or "/opt/running-page")
    state = Path(os.getenv("RUNNING_PAGE_HOST_STATE", "/var/lib/running-page"))
    timers = [f"running-page-{x}.timer" for x in ("update", "backup")]
    enabled = [unit for unit in timers if active(unit)]
    with (state / "upgrade.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for unit in enabled:
            run([*manager(), "stop", unit])
        try:
            deadline = time.monotonic() + 2800
            while any(
                active(f"running-page-{x}.service") for x in ("update", "backup")
            ):
                if time.monotonic() > deadline:
                    raise RuntimeError("An existing operation is still running")
                time.sleep(2)
            run([code_root / "current/ops/run", "backup", "--local-only"])
            target = code_root / "releases" / args.ref
            checkout(target, args.ref)
            environment = dict(
                os.environ,
                RUNNING_PAGE_IMAGE_TAG=args.ref,
                RUNNING_PAGE_REVISION=args.ref,
            )
            compose = ["docker", "compose", "-f", target / "ops/compose.yaml"]
            run([target / "ops/build"], env=environment)
            run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--interactive=false",
                    "--entrypoint",
                    "python",
                    "runner",
                    "-m",
                    "unittest",
                    "tests.test_automation",
                    "tests.test_personal_api",
                    "tests.test_ops",
                    "-q",
                ],
                env=environment,
            )
            run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--interactive=false",
                    "runner",
                    "build",
                ],
                env=environment,
            )
            runtime = target / "ops/runtime.env"
            settings = {
                "RUNNING_PAGE_IMAGE_TAG": args.ref,
                "RUNNING_PAGE_REVISION": args.ref,
            }
            for key in (
                "RUNNING_PAGE_ROOT",
                "RUNNING_PAGE_MEMORY_LIMIT",
                "RUNNING_PAGE_MEMORY_SWAP_LIMIT",
                "RUNNING_PAGE_CPUSET",
                "NODE_IMAGE",
                "PYTHON_IMAGE",
            ):
                if os.getenv(key):
                    settings[key] = os.environ[key]
            runtime.write_text(
                "".join(
                    f"{key}={shlex.quote(value)}\n" for key, value in settings.items()
                )
            )
            runtime.chmod(0o600)
            next_link = code_root / ".next"
            next_link.unlink(missing_ok=True)
            next_link.symlink_to(target)
            os.replace(next_link, code_root / "current")
            print(
                "Upgrade validated and selected; next update publishes the complete candidate."
            )
        finally:
            for unit in enabled:
                run([*manager(), "start", unit])


if __name__ == "__main__":
    main()
