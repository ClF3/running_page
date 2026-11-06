"""Create the project's explicitly requested 2 GiB swap, without replacing files."""

import os
from pathlib import Path
import shutil
import subprocess


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo")
    path = Path("/var/lib/running-page.swap")
    if path.is_symlink():
        raise SystemExit("Swap path is a symlink; refusing to modify it")
    active = subprocess.check_output(
        ["swapon", "--show=NAME", "--noheadings"], text=True
    ).splitlines()
    if str(path) not in active:
        if not path.exists():
            subprocess.run(["fallocate", "-l", "2G", str(path)], check=True)
            path.chmod(0o600)
            subprocess.run(["mkswap", str(path)], check=True)
        else:
            kind = subprocess.check_output(
                ["blkid", "-s", "TYPE", "-o", "value", str(path)], text=True
            ).strip()
            if kind != "swap" or path.stat().st_uid != 0:
                raise SystemExit(
                    "Existing path is not the project's root-owned swap file"
                )
        subprocess.run(["swapon", str(path)], check=True)
    fstab = Path("/etc/fstab")
    line = str(path) + " none swap sw 0 0"
    if line not in fstab.read_text().splitlines():
        previous = Path("/etc/fstab.running-page-before")
        if not previous.exists():
            shutil.copy2(fstab, previous)
        with fstab.open("a") as stream:
            stream.write("\n" + line + "\n")
    print("Dedicated 2 GiB swap is active and configured for reboot")


if __name__ == "__main__":
    main()
