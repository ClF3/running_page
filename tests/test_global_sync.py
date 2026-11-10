"""Cross-region writes use durable receipts, including ambiguous network results."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from run_page.automation.common import Config, read_json
from run_page.automation.activity_files import ActivityFile, OriginalUnavailable
from run_page.automation.global_discovery import (
    discover_activities,
    independent_catalog,
    remember_catalog,
)
from run_page.automation.global_sync import (
    enqueue_activity,
    deliver,
    private_database,
    run_sync,
    sync_status,
    seed_history,
)
from run_page.automation.pipeline import (
    bootstrap,
    synchronize,
    make_backup,
    restore_archive,
)
from tests.test_automation import GPX, RUN_ID, FakeGarmin, seed, summary
from tests.test_personal_api import real_fit


class FakeGlobal:
    account = "test-international-account"

    def __init__(self):
        self.rows = {}
        self.files = {}
        self.uploads = []
        self.updates = []
        self.deletes = []
        self.lose_upload_response = False
        self.lose_delete_response = False
        self.reject_replacement = False
        self.fail_update = False
        self.ignore_update = False
        self.next_id = 1000
        self.hide_next_uploaded_summary = False
        self.hidden_id = None

    async def identity(self):
        return self.account

    async def activities(self):
        return [
            dict(
                activityId=int(key),
                startTimeGMT=value["summaryDTO"]["startTimeGMT"],
                duration=value["summaryDTO"]["duration"],
                distance=value["summaryDTO"]["distance"],
                activityType=value["activityTypeDTO"],
            )
            for key, value in self.rows.items()
        ]

    async def summary(self, target_id):
        if str(target_id) == self.hidden_id:
            self.hidden_id = None
            return None
        return deepcopy(self.rows.get(str(target_id)))

    async def upload(self, path):
        self.uploads.append(str(path))
        if self.reject_replacement:
            return {"id": next(iter(self.rows)), "created": False}
        target_id = str(self.next_id)
        self.next_id += 1
        self.rows[target_id] = summary(target_id)
        self.files[target_id] = Path(path).read_bytes()
        if self.hide_next_uploaded_summary:
            self.hide_next_uploaded_summary = False
            self.hidden_id = target_id
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise RuntimeError("unknown upload outcome")
        return {"id": target_id, "created": True}

    async def update(self, target_id, fields):
        self.updates.append(str(target_id))
        if self.fail_update:
            raise RuntimeError("temporary write failure")
        if self.ignore_update:
            return
        for key, value in fields.items():
            if key == "summaryDTO":
                self.rows[str(target_id)][key].update(value)
            else:
                self.rows[str(target_id)][key] = deepcopy(value)

    async def delete(self, target_id):
        self.deletes.append(str(target_id))
        self.rows.pop(str(target_id), None)
        self.files.pop(str(target_id), None)
        if self.lose_delete_response:
            self.lose_delete_response = False
            raise RuntimeError("unknown delete outcome")

    async def download(self, target_id):
        return ActivityFile("gpx", self.files[str(target_id)])

    async def close(self):
        pass


class FakeAllActivities:
    catalog_scope = "same-personal-key"

    def __init__(self, record, original):
        self.record = record
        self.original = original
        self.queries = []

    async def activities(self, *, all_types=False, start_date=None):
        assert all_types
        self.queries.append(start_date)
        return [
            dict(
                self.record["summaryDTO"],
                activityId=self.record["activityId"],
                activityType=self.record["activityTypeDTO"],
            )
        ]

    async def summary(self, source_id):
        assert source_id == str(self.record["activityId"])
        return deepcopy(self.record)

    async def download(self, source_id):
        assert source_id == str(self.record["activityId"])
        if self.original is None:
            raise OriginalUnavailable("no original")
        return self.original

    async def close(self):
        pass


class GlobalSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seed = self.root / "seed"
        self.seed.mkdir()
        seed(self.seed)
        self.cfg = Config(
            self.root / "absent",
            source=self.root / "code",
            state=self.root / "state",
            global_sync=True,
            global_sync_limit=20,
        )
        self.cfg.source.mkdir()
        bootstrap(self.cfg, self.seed)
        self.remote = FakeGlobal()

    def queue(self, value=None, content=GPX):
        value = value or summary("100")
        path = self.cfg.current / "ORIGINAL_OUT/100.gpx"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return enqueue_activity(
            self.cfg.current / "run_page/data.db",
            "100",
            value,
            "ORIGINAL_OUT/100.gpx",
            original=True,
        )

    def execute(self, **kwargs):
        return asyncio.run(run_sync(self.cfg, self.remote, **kwargs))

    def discover(self, source, *, full=False):
        self.cfg.global_sync_all_activities = True
        catalog = asyncio.run(source.activities(all_types=True))
        discover_activities(self.cfg, catalog, full=full)

    def test_independent_delivery_discovers_a_run_without_a_website_sync(self):
        self.cfg.global_sync_standalone = self.cfg.global_sync_all_activities = True
        original = real_fit(indoor=True, hour=1)
        source = FakeAllActivities(
            summary("200", hour=1), ActivityFile("fit", original)
        )
        website = self.cfg.current / "src/static/activities.json"
        before = website.read_bytes()
        with (
            patch("run_page.automation.garmin.create_client", return_value=source),
            patch(
                "run_page.automation.global_client.GlobalClient",
                return_value=self.remote,
            ),
        ):
            result = deliver(self.cfg)
        self.assertEqual(result["source_scan"], "full")
        self.assertEqual(source.queries, [None])
        self.assertEqual(result["counts"], {"done": 1})
        self.assertEqual(self.remote.files, {"1000": original})
        self.assertEqual(website.read_bytes(), before)

    def test_recent_scan_adds_new_runs_and_reuses_prepared_history(self):
        self.cfg.global_sync_all_activities = True
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        earlier = summary("100")
        existing = dict(
            earlier["summaryDTO"],
            activityId=100,
            activityType=earlier["activityTypeDTO"],
        )
        source = FakeAllActivities(summary("200", hour=1), ActivityFile("gpx", GPX))
        remember_catalog(
            self.cfg,
            source.catalog_scope,
            [existing],
            {"100"},
            now=now - timedelta(minutes=30),
        )
        rows, reusable, mode = asyncio.run(
            independent_catalog(self.cfg, source, now=now)
        )
        self.assertEqual(mode, "recent_with_history")
        self.assertEqual(source.queries, ["2026-10-11"])
        self.assertEqual({r["activityId"] for r in rows}, {100, 200})
        self.assertEqual(reusable, {"100"})

        source.catalog_scope = "different-personal-key"
        rows, reusable, mode = asyncio.run(
            independent_catalog(self.cfg, source, now=now)
        )
        self.assertEqual(mode, "full")
        self.assertEqual(source.queries[-1], None)
        self.assertEqual([r["activityId"] for r in rows], [200])
        self.assertEqual(reusable, set())

    def test_original_indoor_fit_is_delivered_and_edits_stay_private(self):
        record = summary("200")
        record["activityTypeDTO"] = {"typeKey": "strength_training"}
        record["summaryDTO"]["distance"] = 0
        fit = real_fit(indoor=True, sport="TRAINING")
        source = FakeAllActivities(record, ActivityFile("fit", fit))
        public = self.cfg.current / "src/static/activities.json"
        before = public.read_bytes()
        self.discover(source)
        self.assertEqual(
            self.execute(dry_run=True, private_only=True)["plan"], {"new_candidate": 1}
        )
        self.assertEqual(self.remote.uploads, [])

        self.execute(source_client=source, private_only=True)
        self.assertEqual(self.remote.files, {"1000": fit})
        self.assertEqual(
            self.remote.rows["1000"]["activityTypeDTO"]["typeKey"], "strength_training"
        )
        self.assertEqual(sync_status(self.cfg)["other_counts"], {"done": 1})

        record["description"] = "Private training notes"
        self.discover(source, full=True)
        self.execute(source_client=source, private_only=True)
        self.assertEqual(self.remote.rows["1000"]["description"], record["description"])
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(public.read_bytes(), before)
        make_backup(self.cfg, local_only=True)
        recovered = self.root / "public-backup"
        restore_archive(self.cfg.state / "latest-backup.tar.gz", recovered)
        self.assertFalse((recovered / "private").exists())
        self.assertFalse(list(recovered.rglob("*.fit")))

    def test_nonrunning_history_uses_original_bytes_across_activity_types(self):
        record = summary("200")
        record["activityTypeDTO"] = {"typeKey": "hiking"}
        source = FakeAllActivities(record, ActivityFile("gpx", GPX))
        existing = summary("2000")
        existing["activityTypeDTO"] = {"typeKey": "trail_running"}
        self.remote.rows["2000"] = existing
        self.remote.files["2000"] = GPX
        self.discover(source)

        self.execute(source_client=source, private_only=True)
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.deletes, [])
        self.assertEqual(
            self.remote.rows["2000"]["activityTypeDTO"]["typeKey"], "hiking"
        )
        self.assertEqual(sync_status(self.cfg)["other_counts"], {"done": 1})
        self.assertFalse(sync_status(self.cfg)["entries"][0]["owned"])

    def test_nonrunning_never_claims_another_sources_target(self):
        self.queue()
        self.execute()
        record = summary("200")
        record["activityTypeDTO"] = {"typeKey": "cycling"}
        source = FakeAllActivities(record, ActivityFile("gpx", GPX))
        self.discover(source)

        self.execute(source_client=source, private_only=True)
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(
            self.remote.rows["1000"]["activityTypeDTO"]["typeKey"], "running"
        )
        status = sync_status(self.cfg)
        self.assertEqual(status["other_counts"], {"blocked": 1})
        self.assertEqual(
            status["entries"][0]["error"], "destination_mapped_to_another_source"
        )

    def test_activity_without_an_original_is_held(self):
        record = summary("200")
        record["activityTypeDTO"] = {"typeKey": "yoga"}
        source = FakeAllActivities(record, None)
        self.discover(source)
        self.execute(source_client=source, private_only=True)

        self.assertEqual(self.remote.uploads, [])
        status = sync_status(self.cfg)
        self.assertEqual(status["other_counts"], {"blocked": 1})
        self.assertEqual(
            status["entries"][0]["error"], "source_activity_has_no_original"
        )

    def test_private_upload_with_unverified_file_stays_blocked_on_retry(self):
        class WrongOriginal(FakeGlobal):
            async def upload(self, path):
                result = await super().upload(path)
                self.files[result["id"]] = GPX + b"\n"
                return result

        self.remote = WrongOriginal()
        record = summary("200")
        record["activityTypeDTO"] = {"typeKey": "cycling"}
        source = FakeAllActivities(record, ActivityFile("gpx", GPX))
        self.discover(source)

        self.execute(source_client=source, private_only=True)
        self.execute(source_client=source, private_only=True, retry_blocked=True)
        self.assertEqual(sync_status(self.cfg)["other_counts"], {"blocked": 1})
        self.assertEqual(self.remote.updates, [])
        self.assertEqual(len(self.remote.uploads), 1)

    def test_reclassification_preserves_both_activity_ids_and_website_scope(self):
        self.queue()
        self.execute()
        record = summary("100")
        record["activityTypeDTO"] = {"typeKey": "cycling"}
        source = FakeAllActivities(record, ActivityFile("gpx", GPX))
        self.cfg.global_sync_all_activities = True
        catalog = []
        synchronize(self.cfg, full=True, client=source, catalog=catalog)
        self.assertEqual(read_json(self.cfg.current / "src/static/activities.json"), [])
        discover_activities(self.cfg, catalog)
        self.execute(source_client=source)
        self.assertEqual(
            self.remote.rows["1000"]["activityTypeDTO"]["typeKey"], "cycling"
        )

        record["activityTypeDTO"] = {"typeKey": "running"}
        catalog = []
        synchronize(self.cfg, client=source, catalog=catalog)
        self.assertEqual(
            [
                r["run_id"]
                for r in read_json(self.cfg.current / "src/static/activities.json")
            ],
            [RUN_ID],
        )
        discover_activities(self.cfg, catalog)
        self.execute(source_client=source)
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(
            self.remote.rows["1000"]["activityTypeDTO"]["typeKey"], "running"
        )
        with sqlite3.connect(private_database(self.cfg)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT source_id,target_id,owned FROM automation_global_sync"
                ).fetchall(),
                [("100", "1000", 1)],
            )

    def test_uploads_once_and_only_source_changes_trigger_metadata_updates(self):
        self.queue()
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})
        self.queue()
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        changed = summary("100")
        changed["activityName"] = "Changed name"
        changed["summaryDTO"]["distance"] = 1250
        self.queue(changed)
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(self.remote.rows["1000"]["activityName"], "Changed name")
        self.assertEqual(self.remote.rows["1000"]["summaryDTO"]["distance"], 1250)

    def test_existing_activity_is_adopted_without_upload_and_not_owned(self):
        self.remote.rows["2000"] = summary("2000")
        self.queue()
        self.execute()
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})
        self.assertFalse(sync_status(self.cfg)["entries"][0]["owned"])

    def test_same_start_with_changed_statistics_can_match_original_bytes(self):
        existing = summary("2000")
        existing["summaryDTO"]["distance"] *= 1.2
        self.remote.rows["2000"] = existing
        self.remote.files["2000"] = GPX
        self.queue()

        self.execute()

        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})
        self.assertFalse(sync_status(self.cfg)["entries"][0]["owned"])

    def test_unconfirmed_same_start_is_held_for_review_without_upload(self):
        existing = summary("2000")
        existing["summaryDTO"]["distance"] *= 1.2
        self.remote.rows["2000"] = existing
        self.remote.files["2000"] = GPX + b"\n"
        self.queue()

        self.execute()

        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.updates, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_prior_import_with_a_different_sport_type_requires_original_match(self):
        existing = summary("2000")
        existing["activityTypeDTO"] = {"typeKey": "other"}
        self.remote.rows["2000"] = existing
        self.remote.files["2000"] = GPX
        self.queue()

        self.assertEqual(
            self.execute(dry_run=True)["plan"], {"needs_source_verification": 1}
        )
        self.execute()

        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})
        self.assertEqual(
            self.remote.rows["2000"]["activityTypeDTO"]["typeKey"], "running"
        )

    def test_unknown_upload_is_reconciled_instead_of_blindly_reuploaded(self):
        self.queue()
        self.remote.lose_upload_response = True
        self.execute()
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_accepted_upload_receipt_is_resumed_without_another_post(self):
        class AsynchronousGlobal(FakeGlobal):
            ready = False

            async def upload(self, path):
                self.result = await super().upload(path)
                return {"receipt": "/upload-service/status/example"}

            async def upload_status(self, receipt):
                return self.result if self.ready else None

        self.remote = AsynchronousGlobal()
        self.queue()
        with patch("run_page.automation.global_sync.asyncio.sleep"):
            self.assertEqual(self.execute()["failed"], 0)
        self.assertEqual(sync_status(self.cfg)["counts"], {"uploading": 1})

        edited = summary("100")
        edited["activityName"] = "Edited while import was processing"
        self.queue(edited)
        self.remote.ready = True
        self.assertEqual(self.execute()["failed"], 0)
        self.assertEqual(sync_status(self.cfg)["counts"], {"pending": 1})
        self.assertEqual(self.execute()["failed"], 0)
        status = sync_status(self.cfg)
        self.assertEqual(status["counts"], {"done": 1})
        self.assertTrue(status["entries"][0]["owned"])
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(
            self.remote.rows["1000"]["activityName"], edited["activityName"]
        )

    def test_metadata_failure_retries_without_another_upload(self):
        self.queue()
        self.remote.fail_update = True
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.remote.fail_update = False
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_accepted_upload_can_become_visible_later_without_being_lost(self):
        self.queue()
        self.remote.hide_next_uploaded_summary = True
        self.execute()
        self.assertEqual(sync_status(self.cfg)["counts"], {"updating": 1})
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_success_response_with_ignored_changes_does_not_mark_synced(self):
        changed = summary("100")
        changed["activityName"] = "Must be read back"
        self.queue(changed)
        self.remote.ignore_update = True
        self.execute()
        self.assertNotEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_source_trim_is_held_without_overwriting_untrimmed_statistics(self):
        self.remote.rows["2000"] = summary("2000")
        self.remote.files["2000"] = GPX
        cropped = summary("100")
        cropped["activityName"] = "Cropped run"
        cropped["metadataDTO"] = {"trimmed": True}
        cropped["summaryDTO"]["duration"] -= 30
        self.queue(cropped)

        self.execute()

        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.rows["2000"]["activityName"], "Cropped run")
        self.assertEqual(
            self.remote.rows["2000"]["summaryDTO"], summary("2000")["summaryDTO"]
        )
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_new_trimmed_source_is_not_uploaded_as_an_uncropped_file(self):
        cropped = summary("100")
        cropped["metadataDTO"] = {"trimmed": True}
        self.queue(cropped)

        self.execute()

        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_verified_clipped_import_accepts_metadata_but_holds_new_crop(self):
        cropped = summary("100")
        cropped["metadataDTO"] = {"trimmed": True}
        cropped["summaryDTO"]["elapsedDuration"] = 300
        self.queue(cropped)
        self.remote.rows["2000"] = summary("2000")
        self.remote.rows["2000"]["summaryDTO"]["elapsedDuration"] = 300
        self.remote.files["2000"] = b"verified clipped file"
        target_hash = sha256(self.remote.files["2000"]).hexdigest()
        database = self.cfg.current / "run_page/data.db"
        with sqlite3.connect(database) as db:
            desired = db.execute("SELECT desired FROM automation_global_sync").fetchone()
            confirmed = json.loads(desired[0])
            confirmed["target_file_hash"] = target_hash
            db.execute(
                "UPDATE automation_global_sync "
                "SET confirmed=?, target_id='2000', owned=1, phase='done'",
                (json.dumps(confirmed),),
            )

        cropped["activityName"] = "Renamed run"
        self.queue(cropped)
        self.execute()
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})
        self.assertEqual(self.remote.rows["2000"]["activityName"], "Renamed run")
        with sqlite3.connect(database) as db:
            receipt = db.execute("SELECT confirmed FROM automation_global_sync").fetchone()
        self.assertEqual(json.loads(receipt[0])["target_file_hash"], target_hash)

        recropped = deepcopy(cropped)
        recropped["summaryDTO"]["elapsedDuration"] = 290
        untrimmed = summary("100")
        for edited in (recropped, untrimmed):
            edited["summaryDTO"]["distance"] = 1000
            self.queue(edited)
            self.execute()
            self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})
            self.assertEqual(self.remote.rows["2000"]["summaryDTO"]["distance"], 1100)
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.deletes, [])

    def test_ambiguous_existing_activities_are_not_overwritten(self):
        self.remote.rows = {"2000": summary("2000"), "2001": summary("2001")}
        self.queue()
        self.execute()
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.updates, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_account_switch_is_rejected_before_any_writes(self):
        self.queue()
        self.execute()
        self.remote.account = "different-account"
        changed = summary("100")
        changed["activityName"] = "Another source version"
        self.queue(changed)
        with self.assertRaisesRegex(RuntimeError, "account"):
            self.execute()
        self.assertEqual(len(self.remote.uploads), 1)

    def test_snapshot_failure_never_enqueues_partial_source_data(self):
        before = self.cfg.current
        with self.assertRaises(RuntimeError):
            synchronize(self.cfg, client=FakeGarmin(ids=("100", "200"), fail="200"))
        self.assertEqual(self.cfg.current, before)
        self.assertEqual(sync_status(self.cfg)["counts"], {})

    def test_normal_scan_and_pending_retry_do_not_require_visible_page_changes(self):
        synchronize(self.cfg, client=FakeGarmin())
        self.assertEqual(sync_status(self.cfg)["counts"], {"pending": 1})
        self.execute()
        synchronize(self.cfg, client=FakeGarmin())
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)

    def test_dry_run_never_writes_to_destination(self):
        self.queue()
        planned = self.execute(dry_run=True)
        self.assertEqual(planned["plan"], {"new_candidate": 1})
        self.remote.rows["2000"] = summary("2000")
        planned = self.execute(dry_run=True)
        self.assertEqual(planned["plan"], {"existing_match": 1})
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.updates, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"pending": 1})

    def test_replacement_verifies_new_file_before_retiring_owned_previous(self):
        self.queue()
        self.execute()
        self.queue(content=GPX + b"\n")
        self.execute()
        self.assertEqual(len(self.remote.uploads), 2)
        self.assertEqual(self.remote.deletes, ["1000"])
        self.assertEqual(set(self.remote.rows), {"1001"})
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_existing_adopted_activities_are_never_deleted(self):
        self.remote.rows["2000"] = summary("2000")
        self.queue()
        self.execute()
        self.queue(content=GPX + b"\n")
        self.execute()
        self.assertEqual(self.remote.uploads, [])
        self.assertEqual(self.remote.deletes, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_replacement_duplicate_keeps_old_activity_and_remains_incomplete(self):
        self.queue()
        self.execute()
        self.queue(content=GPX + b"\n")
        self.remote.reject_replacement = True
        self.execute()
        self.assertEqual(self.remote.deletes, [])
        self.assertEqual(set(self.remote.rows), {"1000"})
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_lost_delete_response_resumes_without_uploading_a_third_version(self):
        self.queue()
        self.execute()
        self.queue(content=GPX + b"\n")
        self.remote.lose_delete_response = True
        self.execute()
        self.execute()
        self.assertEqual(len(self.remote.uploads), 2)
        self.assertEqual(self.remote.deletes, ["1000"])
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_lost_replacement_upload_response_is_reconciled_before_retirement(self):
        self.queue()
        self.execute()
        self.queue(content=GPX + b"\n")
        self.remote.lose_upload_response = True
        self.execute()
        self.execute()
        self.assertEqual(len(self.remote.uploads), 2)
        self.assertEqual(self.remote.deletes, ["1000"])
        self.assertEqual(sync_status(self.cfg)["counts"], {"done": 1})

    def test_source_change_during_unknown_upload_is_not_falsely_confirmed(self):
        self.queue()
        self.remote.lose_upload_response = True
        self.execute()
        self.queue(content=GPX + b"\n")
        self.execute()
        self.assertEqual(len(self.remote.uploads), 1)
        self.assertEqual(self.remote.deletes, [])
        self.assertEqual(sync_status(self.cfg)["counts"], {"blocked": 1})

    def test_description_change_is_delivered_when_web_json_has_not_changed(self):
        class WithDescription(FakeGarmin):
            async def summary(self, source_id):
                return {**summary(source_id), "description": "Source note changed"}

        synchronize(self.cfg, client=FakeGarmin())
        self.execute()
        before = read_json(self.cfg.current / "src/static/activities.json")
        synchronize(self.cfg, client=WithDescription())
        self.assertEqual(
            read_json(self.cfg.current / "src/static/activities.json"), before
        )
        self.execute()
        self.assertEqual(self.remote.rows["1000"]["description"], "Source note changed")
        synchronize(self.cfg, client=FakeGarmin())
        self.execute()
        self.assertEqual(self.remote.rows["1000"]["description"], "")
        self.assertEqual(len(self.remote.uploads), 1)

    def test_queue_receipts_survive_complete_backup_and_restore(self):
        self.queue()
        self.execute()
        make_backup(self.cfg, local_only=True)
        restored = self.root / "restored"
        restore_archive(self.cfg.state / "latest-backup.tar.gz", restored)
        with sqlite3.connect(restored / "run_page/data.db") as db:
            self.assertEqual(
                db.execute(
                    "SELECT target_id,owned,phase FROM automation_global_sync"
                ).fetchone(),
                ("1000", 1, "done"),
            )
        self.assertEqual((restored / "ORIGINAL_OUT/100.gpx").read_bytes(), GPX)

    def test_first_history_queue_includes_previously_downloaded_files(self):
        self.assertEqual(seed_history(self.cfg), 1)
        self.assertEqual(seed_history(self.cfg), 0)
        self.execute(source_client=FakeGarmin())
        self.assertEqual(len(self.remote.uploads), 1)


if __name__ == "__main__":
    unittest.main()
