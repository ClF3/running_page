"""Render system or user units without changing unrelated services."""

from pathlib import Path
import shlex
import sys


def install_units(source, units, code_root, user_mode, *, only=None):
    source, units, code_root = Path(source), Path(units), Path(code_root)
    units.mkdir(parents=True, exist_ok=True)
    for template in (source / "ops/systemd").glob("running-page-*"):
        if only is not None and template.name not in only:
            continue
        text = template.read_text().replace("/opt/running-page", str(code_root))
        if user_mode:
            text = (
                "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.startswith(("After=", "Wants=", "Requires="))
                )
                + "\n"
            )
        target = units / template.name
        target.write_text(text)
        target.chmod(0o644)
    if user_mode:
        runtime = source / "ops/runtime.env"
        lines = runtime.read_text().splitlines() if runtime.exists() else []
        lines = [line for line in lines if not line.startswith("RUNNING_PAGE_ROOT=")]
        lines.append(f"RUNNING_PAGE_ROOT={shlex.quote(str(code_root))}")
        runtime.write_text("\n".join(lines) + "\n")
        runtime.chmod(0o600)


if __name__ == "__main__":
    install_units(
        *sys.argv[1:4], user_mode=sys.argv[4] == "1", only=sys.argv[5:] or None
    )
