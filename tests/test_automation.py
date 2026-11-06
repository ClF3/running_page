"""Exercise the real data pipeline and remote failures without real credentials."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from run_page.automation.common import (
    Config,
    atomic_write,
    job_lock,
    read_json,
    write_json,
)
from run_page.automation.garmin import (
    GarminClient,
    select_ids,
    source_rows,
)
from run_page.automation.pipeline import (
    bootstrap,
    make_backup,
    prepare,
    probe,
    publish,
    push_backup,
    restore_archive,
    synchronize,
)
from generator.db import Activity, init_db
from utils import write_activities_files

START = datetime(2026, 11, 6, tzinfo=timezone.utc)
RUN_ID = int(START.timestamp() * 1000)
GPX = b"""<?xml version="1.0"?><gpx version="1.1" creator="test"
 xmlns="http://www.topografix.com/GPX/1/1"><extensions>
 <start_time>2026-11-06T00:00:00+00:00</start_time>
 <end_time>2026-11-06T00:05:00+00:00</end_time><distance>1000</distance>
 <moving_time>300</moving_time><elapsed_time>300</elapsed_time><average_speed>3.3</average_speed>
 </extensions></gpx>"""


def seed(root):
    (root / "run_page").mkdir(parents=True)
    (root / "GPX_OUT").mkdir()
    (root / "GPX_OUT/100.gpx").write_bytes(GPX)
    (root / "assets").mkdir()
    (root / "assets/github.svg").write_text("<svg>old</svg>")
    session = init_db(str(root / "run_page/data.db"))
    row = Activity(
        run_id=RUN_ID,
        name="Original",
        distance=1000,
        moving_time=timedelta(seconds=300),
        elapsed_time=timedelta(seconds=300),
        type="Run",
        subtype="running",
        start_date="2026-11-06 00:00:00",
        start_date_local="2026-11-06 08:00:00",
        location_country="",
        summary_polyline="",
        average_speed=3.3,
        elevation_gain=0,
    )
    session.add(row)
    session.commit()
    row.streak = 1
    write_activities_files([row.to_dict()], root / "src/static/activities.json")
    session.close()
    write_json(root / "imported.json", ["100.gpx"])


def summary(source_id, hour=0):
    return {
        "activityId": int(source_id),
        "activityName": "Corrected",
        "activityTypeDTO": {"typeKey": "running"},
        "summaryDTO": {
            "distance": 1100,
            "duration": 300,
            "movingDuration": 290,
            "averageSpeed": 3.8,
            "averageHR": 155,
            "startTimeGMT": f"2026-11-06T0{hour}:00:00Z",
            "startTimeLocal": f"2026-11-06T0{hour + 8}:00:00",
        },
    }


class FakeGarmin:
    def __init__(self, ids=("100",), fail=None, hour=0):
        self.ids, self.fail, self.hour = ids, fail, hour

    async def activities(self):
        return [
            {"activityId": int(i), "startTimeGMT": "2026-11-06T00:00:00Z"}
            for i in self.ids
        ]

    async def summary(self, source_id):
        return summary(source_id, self.hour)

    async def download(self, source_id):
        if source_id == self.fail:
            raise RuntimeError("simulated interrupted download")
        return GPX


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("print('version 1')\n")
        self.seed = self.root / "seed"
        seed(self.seed)
        self.cfg = Config(source=self.source, state=self.root / "state")
        bootstrap(self.cfg, self.seed)

    def test_atomic_write_preserves_old_file_on_interruption(self):
        path = self.root / "value"
        path.write_text("old")
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.glob(".tmp-*")), [])

    def test_bootstrap_is_independent_and_cannot_overwrite_live_data(self):
        (self.seed / "GPX_OUT/100.gpx").write_text("broken")
        self.assertEqual((self.cfg.current / "GPX_OUT/100.gpx").read_bytes(), GPX)
        with self.assertRaises(RuntimeError):
            bootstrap(self.cfg, self.seed)

    def test_mapping_binds_legacy_files_to_existing_database_ids(self):
        self.assertEqual(
            source_rows(self.cfg.current / "run_page/data.db")["100"]["run_id"], RUN_ID
        )

    def test_new_historic_records_are_selected_and_moved_dates_are_rechecked(self):
        old = (START - timedelta(days=100)).isoformat()
        remote = [
            {"activityId": 1, "startTimeGMT": old},
            {"activityId": 2, "startTimeGMT": old},
        ]
        sources = {"1": {"run_id": 1, "start_time": START.isoformat(), "ignored": 0}}
        self.assertEqual(
            select_ids(remote, sources, START - timedelta(days=30)), ["1", "2"]
        )

    def test_old_known_records_require_full_refresh(self):
        old = (START - timedelta(days=100)).isoformat()
        sources = {"1": {"start_time": old, "ignored": 0}}
        remote = [{"activityId": 1, "startTimeGMT": old}]
        self.assertEqual(select_ids(remote, sources, START - timedelta(days=30)), [])
        self.assertEqual(
            select_ids(remote, sources, START - timedelta(days=30), True), ["1"]
        )

    def test_changed_start_time_updates_original_record_without_duplicate(self):
        synchronize(self.cfg, full=True, client=FakeGarmin(hour=1))
        rows = read_json(self.cfg.current / "src/static/activities.json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], RUN_ID)
        self.assertEqual(rows[0]["distance"], 1100)
        self.assertEqual(rows[0]["average_heartrate"], 155)
        self.assertEqual(rows[0]["start_date_local"], "2026-11-06 09:00:00")

    def test_partial_download_failure_cannot_promote_any_changes(self):
        old = self.cfg.current
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            synchronize(
                self.cfg, full=True, client=FakeGarmin(("100", "200"), fail="200")
            )
        self.assertEqual(self.cfg.current, old)
        self.assertEqual(
            read_json(old / "src/static/activities.json")[0]["name"], "Original"
        )
        self.assertEqual((old / "GPX_OUT/100.gpx").read_bytes(), GPX)
        self.assertEqual(len(list((self.cfg.state / "snapshots").iterdir())), 1)

    def test_bookkeeping_failure_cannot_delete_promoted_data(self):
        def fail_sync_record(path, value):
            if Path(path).name == "sync.json":
                raise OSError("simulated full disk after promotion")
            write_json(path, value)

        with patch(
            "run_page.automation.pipeline.write_json", side_effect=fail_sync_record
        ):
            with self.assertRaises(OSError):
                synchronize(self.cfg, full=True, client=FakeGarmin(hour=1))
        self.assertTrue(self.cfg.current.is_dir())
        self.assertEqual(
            read_json(self.cfg.current / "src/static/activities.json")[0]["distance"],
            1100,
        )

    def test_route_revision_replaces_route_and_location(self):
        client = FakeGarmin()

        async def outdoor(_):
            return b"""<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
            <trk><name>GPS</name><trkseg>
            <trkpt lat="30.0" lon="120.0"><time>2026-11-06T00:00:00Z</time></trkpt>
            <trkpt lat="30.001" lon="120.001"><time>2026-11-06T00:00:07Z</time></trkpt>
            </trkseg></trk></gpx>"""

        client.download = outdoor
        with patch("generator.db.g.reverse", return_value="Test location"):
            synchronize(self.cfg, full=True, client=client)
        row = read_json(self.cfg.current / "src/static/activities.json")[0]
        self.assertTrue(row["summary_polyline"])
        self.assertEqual(row["location_country"], "Test location")
        self.assertEqual(row["run_id"], RUN_ID)

    def make_candidate(self):
        item = {"input_hash": "a" * 64, "code_hash": "b" * 64, "built_at": "test"}
        directory = self.cfg.state / "releases" / item["input_hash"] / "dist"
        directory.mkdir(parents=True)
        write_json(directory / "release.json", item)
        from run_page.automation.common import content_manifest

        write_json(directory.parent / "files.json", content_manifest(directory))
        write_json(self.cfg.state / "candidate.json", item)
        return item

    def test_publish_failure_keeps_candidate_and_does_not_mark_success(self):
        self.make_candidate()
        domain = {
            "success": True,
            "result": [{"hostname": "run-dev.clf3.org", "service": "running-page"}],
        }
        with (
            patch.dict(
                os.environ,
                {"CLOUDFLARE_API_TOKEN": "test-token", "CLOUDFLARE_ACCOUNT_ID": "test"},
            ),
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(domain).encode()),
            ),
            patch("run_page.automation.pipeline.probe", return_value=False),
            patch(
                "run_page.automation.pipeline.run",
                side_effect=RuntimeError("upload failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                publish(self.cfg)
        self.assertTrue((self.cfg.state / "candidate.json").exists())
        self.assertFalse((self.cfg.state / "deployed.json").exists())

    def test_unknown_upload_outcome_is_reconciled_without_uploading_twice(self):
        item = self.make_candidate()
        domain = {
            "success": True,
            "result": [{"hostname": "run-dev.clf3.org", "service": "running-page"}],
        }
        with (
            patch.dict(
                os.environ,
                {"CLOUDFLARE_API_TOKEN": "test-token", "CLOUDFLARE_ACCOUNT_ID": "test"},
            ),
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(domain).encode()),
            ),
            patch("run_page.automation.pipeline.probe", return_value=True),
            patch("run_page.automation.pipeline.run") as upload,
        ):
            publish(self.cfg)
        upload.assert_not_called()
        self.assertEqual(
            read_json(self.cfg.state / "deployed.json")["input_hash"],
            item["input_hash"],
        )

    def test_release_probe_identifies_itself_to_cloudflare_integrity_check(self):
        import urllib.error

        item = self.make_candidate()

        def respond(request, **kwargs):
            if request.get_header("User-agent") != "running-page-deployer/1.0":
                raise urllib.error.HTTPError(
                    request.full_url, 403, "error 1010", {}, None
                )
            return io.BytesIO(json.dumps(item).encode())

        with patch("urllib.request.urlopen", side_effect=respond):
            self.assertTrue(probe(self.cfg, item))

    def test_uploaded_version_can_appear_after_delayed_probes(self):
        item = self.make_candidate()
        domain = {
            "success": True,
            "result": [{"hostname": "run-dev.clf3.org", "service": "running-page"}],
        }
        with (
            patch.dict(
                os.environ,
                {"CLOUDFLARE_API_TOKEN": "test-token", "CLOUDFLARE_ACCOUNT_ID": "test"},
            ),
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(domain).encode()),
            ),
            patch(
                "run_page.automation.pipeline.probe",
                side_effect=[False, False, False, True],
            ),
            patch("run_page.automation.pipeline.run") as upload,
            patch("run_page.automation.pipeline.time.sleep") as sleep,
        ):
            publish(self.cfg)
        upload.assert_called_once()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(
            read_json(self.cfg.state / "deployed.json")["input_hash"],
            item["input_hash"],
        )

    def test_exhausted_online_verification_preserves_pending_release(self):
        self.make_candidate()
        domain = {
            "success": True,
            "result": [{"hostname": "run-dev.clf3.org", "service": "running-page"}],
        }
        with (
            patch.dict(
                os.environ,
                {"CLOUDFLARE_API_TOKEN": "test-token", "CLOUDFLARE_ACCOUNT_ID": "test"},
            ),
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(domain).encode()),
            ),
            patch("run_page.automation.pipeline.probe", return_value=False) as checks,
            patch("run_page.automation.pipeline.run") as upload,
            patch("run_page.automation.pipeline.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not yet verified"):
                publish(self.cfg)
        upload.assert_called_once()
        self.assertEqual(checks.call_count, 5)
        self.assertTrue((self.cfg.state / "candidate.json").exists())
        self.assertFalse((self.cfg.state / "deployed.json").exists())

    def test_backup_roundtrip_excludes_credentials_and_detects_tampering(self):
        (self.source / ".env").write_text("SECRET=do-not-back-up")
        (self.source / "run_page").mkdir()
        (self.source / "run_page/data.db.backup-secret").write_text("private")
        make_backup(self.cfg, local_only=True)
        target = self.root / "restored"
        restore_archive(self.cfg.state / "latest-backup.tar.gz", target)
        self.assertFalse((target / ".env").exists())
        self.assertFalse((target / "run_page/data.db.backup-secret").exists())
        self.assertEqual((target / "GPX_OUT/100.gpx").read_bytes(), GPX)
        self.assertEqual((target / "app.py").read_text(), "print('version 1')\n")
        with self.assertRaises(RuntimeError):
            restore_archive(self.cfg.state / "latest-backup.tar.gz", target)

    def test_failed_new_backup_does_not_remove_last_good_archive(self):
        make_backup(self.cfg, local_only=True)
        previous = (self.cfg.state / "latest-backup.tar.gz").read_bytes()
        with patch(
            "run_page.automation.pipeline.restore_archive",
            side_effect=RuntimeError("checksum error"),
        ):
            with self.assertRaises(RuntimeError):
                make_backup(self.cfg, local_only=True)
        self.assertEqual(
            (self.cfg.state / "latest-backup.tar.gz").read_bytes(), previous
        )

    def test_restore_rejects_archive_path_traversal(self):
        archive = self.root / "malicious.tar"
        with tarfile.open(archive, "w") as stream:
            item = tarfile.TarInfo("../escape")
            item.size = 1
            stream.addfile(item, io.BytesIO(b"x"))
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            restore_archive(archive, self.root / "target")
        self.assertFalse((self.root / "escape").exists())

    def test_daily_backup_has_no_empty_commits_and_failed_push_retries(self):
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True
        )
        self.cfg.repository = str(remote)
        command = (
            "ssh -i /config/github_ed25519 -o UserKnownHostsFile=/config/known_hosts"
        )
        with (
            patch.dict(os.environ, {"GIT_SSH_COMMAND": command}),
            patch("subprocess.run", wraps=subprocess.run) as git_commands,
        ):
            first = make_backup(self.cfg)
            push_backup(self.cfg)
        for call in git_commands.call_args_list:
            if call.args[0][1] in {"ls-remote", "push"}:
                self.assertEqual(call.kwargs["env"]["GIT_SSH_COMMAND"], command)
        second = make_backup(self.cfg)
        self.assertEqual(first["commit"], second["commit"])
        with patch(
            "run_page.automation.pipeline.run",
            side_effect=RuntimeError("network failure"),
        ):
            with self.assertRaises(RuntimeError):
                push_backup(self.cfg)
        self.assertTrue((self.cfg.state / "backup-pending.json").exists())
        push_backup(self.cfg)
        self.assertFalse((self.cfg.state / "backup-pending.json").exists())
        recovered = self.root / "recovered-git"
        subprocess.run(
            ["git", "clone", "-b", "data-backup", str(remote), str(recovered)],
            check=True,
            capture_output=True,
        )
        self.assertTrue((recovered / "run_page/data.db").exists())

    def test_remote_activity_ids_cannot_escape_snapshot_directory(self):
        with self.assertRaisesRegex(RuntimeError, "Invalid Garmin"):
            select_ids(
                [{"activityId": "../../outside", "startTimeGMT": START.isoformat()}],
                {},
                START,
            )

    def test_invalid_gpx_does_not_promote_snapshot(self):
        client = FakeGarmin()

        async def invalid(_):
            return b"<html>service unavailable</html>"

        client.download = invalid
        old = self.cfg.current
        with self.assertRaisesRegex(RuntimeError, "not GPX"):
            synchronize(self.cfg, full=True, client=client)
        self.assertEqual(self.cfg.current, old)

    def test_build_reuses_complete_candidate_and_skips_successful_input(self):
        def build(args, **kwargs):
            out = Path(kwargs["cwd"]) / "dist"
            out.mkdir()
            (out / "index.html").write_text("<html></html>")

        with (
            patch("run_page.automation.pipeline.render") as renderer,
            patch("run_page.automation.pipeline.run", side_effect=build) as builder,
        ):
            first = prepare(self.cfg)
            self.assertEqual(prepare(self.cfg), first)
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(renderer.call_count, 1)
            write_json(self.cfg.state / "deployed.json", first)
            self.assertIsNone(prepare(self.cfg))
            self.assertEqual(builder.call_count, 1)

    def test_new_day_without_visible_svg_change_does_not_rebuild(self):
        def build(args, **kwargs):
            out = Path(kwargs["cwd"]) / "dist"
            out.mkdir()
            (out / "index.html").write_text("<html></html>")

        with (
            patch("run_page.automation.pipeline.datetime") as clock,
            patch("run_page.automation.pipeline.render") as renderer,
            patch("run_page.automation.pipeline.run", side_effect=build) as builder,
        ):
            clock.now.return_value = START
            first = prepare(self.cfg)
            write_json(self.cfg.state / "deployed.json", first)
            clock.now.return_value = START + timedelta(days=1)
            self.assertIsNone(prepare(self.cfg))
            self.assertEqual(renderer.call_count, 2)
            self.assertEqual(builder.call_count, 1)

    def test_failed_build_keeps_previous_complete_candidate(self):
        first = self.make_candidate()
        with (
            patch("run_page.automation.pipeline.render"),
            patch(
                "run_page.automation.pipeline.run",
                side_effect=RuntimeError("build interrupted"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "build interrupted"):
                prepare(self.cfg)
        self.assertEqual(read_json(self.cfg.state / "candidate.json"), first)

    def test_successful_build_bounds_pending_and_published_retention(self):
        old = self.make_candidate()
        for index in (1, 2, 3):
            path = self.cfg.state / "releases" / (str(index) * 64)
            path.mkdir()
            write_json(path / "published.json", {"deployed_at": f"2026-11-0{index}"})

        def build(args, **kwargs):
            out = Path(kwargs["cwd"]) / "dist"
            out.mkdir()
            (out / "index.html").write_text("<html></html>")

        with (
            patch("run_page.automation.pipeline.render"),
            patch("run_page.automation.pipeline.run", side_effect=build),
        ):
            current = prepare(self.cfg)
        self.assertEqual(
            {p.name for p in (self.cfg.state / "releases").iterdir()},
            {"2" * 64, "3" * 64, current["input_hash"]},
        )
        self.assertNotEqual(current["input_hash"], old["input_hash"])

    def test_refuses_corrupt_build_or_production_target_before_network(self):
        item = self.make_candidate()
        file = self.cfg.state / "releases" / item["input_hash"] / "dist/index.html"
        file.write_text("corrupt unrecorded asset")
        with patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "checksums"):
                publish(self.cfg)
        network.assert_not_called()
        file.unlink()
        self.cfg.site_url = "https://run.clf3.org"
        with self.assertRaisesRegex(RuntimeError, "production cutover"):
            publish(self.cfg, dry_run=True)

    def test_corrupt_archive_is_rejected_by_restore(self):
        make_backup(self.cfg, local_only=True)
        archive = self.root / "corrupt.tar"
        with (
            tarfile.open(self.cfg.state / "latest-backup.tar.gz") as original,
            tarfile.open(archive, "w") as output,
        ):
            for member in original.getmembers():
                content = (
                    original.extractfile(member).read() if member.isfile() else None
                )
                if member.name == "app.py":
                    content = b"changed"
                    member.size = len(content)
                output.addfile(
                    member, io.BytesIO(content) if content is not None else None
                )
        with self.assertRaisesRegex(RuntimeError, "checksums"):
            restore_archive(archive, self.root / "bad-restore")

    def test_refuses_backup_to_main_source_branch(self):
        with self.assertRaises(ValueError):
            Config(state=self.root / "bad", backup_branch="master")

    def cli(self, *args):
        from run_page.automation.__main__ import main

        config = self.root / "config.json"
        write_json(config, {"state": str(self.cfg.state), "source": str(self.source)})
        with patch("run_page.automation.__main__.configure_logging"):
            return main(["--config", str(config), *args])

    def test_overlapping_job_is_skipped_without_recording_a_failure(self):
        with job_lock(self.cfg):
            self.assertEqual(self.cli("build"), 0)
        self.assertFalse((self.cfg.state / "jobs.json").exists())

    def test_abandoned_backup_staging_is_removed_but_backup_git_is_preserved(self):
        for name in (
            "backup-abcdefgh",
            "verify-abcd1234",
            "restore-git-abcd1234",
            "backup-git",
        ):
            (self.cfg.state / name).mkdir()
        archive = self.cfg.state / "latest-backup.tar.gz"
        archive.write_bytes(b"last-good")
        archive.with_suffix(".gz.tmp").write_bytes(b"interrupted")
        with patch("run_page.automation.__main__.prepare", return_value=None):
            self.assertEqual(self.cli("build"), 0)
        self.assertEqual(archive.read_bytes(), b"last-good")
        self.assertTrue((self.cfg.state / "backup-git").exists())
        self.assertFalse((self.cfg.state / "backup-abcdefgh").exists())
        self.assertFalse((self.cfg.state / "verify-abcd1234").exists())
        self.assertFalse((self.cfg.state / "restore-git-abcd1234").exists())
        self.assertFalse(archive.with_suffix(".gz.tmp").exists())

    def test_rollback_requires_a_retained_successful_release(self):
        item = self.make_candidate()
        with patch("run_page.automation.__main__.publish", return_value=item) as upload:
            self.assertEqual(self.cli("rollback", "--to", item["input_hash"]), 1)
            upload.assert_not_called()
            write_json(
                self.cfg.state / "releases" / item["input_hash"] / "published.json",
                item,
            )
            self.assertEqual(self.cli("rollback", "--to", item["input_hash"]), 0)
            upload.assert_called_once()


class HttpRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_transient_error_then_success_returns_success(self):
        import httpx

        client = GarminClient.__new__(GarminClient)
        client.garth = type(
            "G", (), {"client": type("C", (), {"oauth2_token": "secret"})()}
        )()
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("asyncio.sleep"):
            self.assertEqual(await client.request("/test"), {"ok": True})
        self.assertEqual(len(calls), 2)
        await client.close()


if __name__ == "__main__":
    unittest.main()
