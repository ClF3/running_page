"""Host deployment must also work without root or system-level Docker units."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops.install_units import install_units
from ops.upgrade import checkout


class HostDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        templates = self.source / "ops/systemd"
        templates.mkdir(parents=True)
        original = Path(__file__).resolve().parents[1] / "ops/systemd"
        for path in original.iterdir():
            (templates / path.name).write_text(path.read_text())

    def test_user_units_use_deployment_directory_and_preserve_runtime_version(self):
        runtime = self.source / "ops/runtime.env"
        runtime.write_text("RUNNING_PAGE_IMAGE_TAG=reviewed-version\n")
        units = self.root / "units"
        code_root = self.root / "deployment"
        install_units(self.source, units, code_root, user_mode=True)
        service = (units / "running-page-update.service").read_text()
        self.assertIn(f"ExecStart={code_root}/current/ops/run update", service)
        self.assertNotIn("docker.service", service)
        self.assertIn("RUNNING_PAGE_IMAGE_TAG=reviewed-version", runtime.read_text())
        self.assertIn(f"RUNNING_PAGE_ROOT={code_root}", runtime.read_text())
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        timer = (units / "running-page-backup.timer").read_text()
        self.assertIn("03:00:00 Asia/Shanghai", timer)
        self.assertIn("Persistent=true", timer)

    def test_system_units_keep_docker_ordering(self):
        units = self.root / "system-units"
        install_units(self.source, units, "/opt/running-page", user_mode=False)
        self.assertIn(
            "Requires=docker.service",
            (units / "running-page-update.service").read_text(),
        )

    def test_host_environment_uses_persisted_user_paths_and_uid(self):
        (self.source / "ops/runtime.env").write_text(
            f"RUNNING_PAGE_ROOT={self.root}/deployment\n"
        )
        environment = Path(__file__).resolve().parents[1] / "ops/environment"
        result = subprocess.run(
            [
                "sh",
                "-c",
                'project_root="$1"; . "$2"; printf "%s\\n" "$RUNNING_PAGE_HOST_STATE" "$RUNNING_PAGE_CONTAINER_USER"',
                "test",
                str(self.source),
                str(environment),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            [f"{self.root}/deployment/state", f"{os.getuid()}:{os.getgid()}"],
        )
        self.assertEqual(result.stderr, "")

    def test_interrupted_upgrade_checkout_can_retry(self):
        target = self.root / "versions/reviewed"
        with patch("ops.upgrade.run", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                checkout(target, "a" * 40)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])
