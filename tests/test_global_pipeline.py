"""Website publication and international delivery fail independently."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, patch

from run_page.automation import __main__ as cli
from run_page.automation.common import Busy, job_lock, read_json


class DeliveryPipelineTest(unittest.TestCase):
    def run_case(
        self,
        source_error=False,
        publish_error=False,
        global_error=False,
        standalone=False,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "state": str(root / "state"),
                        "source": str(root / "code"),
                        "global_sync": True,
                        "global_sync_standalone": standalone,
                    }
                )
            )
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(cli, "configure_logging"))
                source = stack.enter_context(
                    patch.object(
                        cli,
                        "synchronize",
                        side_effect=(
                            RuntimeError("source unavailable") if source_error else None
                        ),
                        return_value={"activities": 1},
                    )
                )
                stack.enter_context(patch.object(cli, "prepare", return_value=None))
                publish = stack.enter_context(
                    patch.object(
                        cli,
                        "publish",
                        side_effect=(
                            RuntimeError("publish unavailable")
                            if publish_error
                            else None
                        ),
                        return_value={"skipped": "no visible changes"},
                    )
                )
                deliver = stack.enter_context(
                    patch.object(
                        cli,
                        "deliver_global",
                        side_effect=(
                            RuntimeError("destination unavailable")
                            if global_error
                            else None
                        ),
                        return_value={
                            "processed": 1,
                            "failed": 0,
                            "counts": {"done": 1},
                        },
                    )
                )
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                code = cli.main(["--config", str(cfg), "update"])
            jobs = read_json(root / "state/jobs.json")
            return code, jobs, publish.call_count, deliver.call_count

    def test_global_failure_does_not_block_website_or_discard_failure_status(self):
        code, jobs, published, delivered = self.run_case(global_error=True)
        self.assertEqual(code, 0)
        self.assertEqual(published, 1)
        self.assertEqual(delivered, 1)
        self.assertEqual(jobs["update"]["consecutive_failures"], 0)
        self.assertEqual(jobs["global-sync"]["consecutive_failures"], 1)

    def test_pending_delivery_retries_even_when_source_cannot_be_reached(self):
        code, jobs, published, delivered = self.run_case(source_error=True)
        self.assertEqual(code, 1)
        self.assertEqual(delivered, 1)
        self.assertEqual(jobs["global-sync"]["consecutive_failures"], 0)

    def test_pending_delivery_retries_even_when_publication_fails(self):
        code, jobs, published, delivered = self.run_case(publish_error=True)
        self.assertEqual(code, 1)
        self.assertEqual(delivered, 1)
        self.assertEqual(jobs["global-sync"]["consecutive_failures"], 0)

    def test_website_task_leaves_delivery_to_the_independent_timer(self):
        for source_error in (False, True):
            with self.subTest(source_error=source_error):
                _, jobs, _, delivered = self.run_case(
                    source_error=source_error, standalone=True
                )
                self.assertEqual(delivered, 0)
                self.assertNotIn("global-sync", jobs)

    def run_scheduled(self, *, retryable=0, busy=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "state": str(root / "state"),
                        "source": str(root / "code"),
                        "global_sync": True,
                        "global_sync_standalone": True,
                    }
                )
            )

            def locked(config, name="runner.lock", **kwargs):
                if busy and name == "runner.lock":
                    raise Busy("busy")
                return job_lock(config, name, **kwargs)

            with (
                patch.object(cli, "configure_logging"),
                patch.object(cli, "job_lock", side_effect=locked) as lock,
                patch.object(
                    cli,
                    "deliver_global",
                    return_value={
                        "failed": 4 + retryable,
                        "retryable_failures": retryable,
                        "counts": {"blocked": 4},
                    },
                ) as deliver,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = cli.main(["--config", str(cfg), "global-sync", "--scheduled"])
            lock.assert_any_call(ANY, wait_seconds=1200)
            return (
                code,
                read_json(root / "state/jobs.json")["global-sync"],
                deliver.call_count,
            )

    def test_timer_keeps_review_items_separate_from_job_failures(self):
        code, status, delivered = self.run_scheduled()
        self.assertEqual((code, delivered), (0, 1))
        self.assertEqual(status["mode"], "scheduled")
        self.assertEqual(status["result"]["counts"], {"blocked": 4})
        self.assertEqual(status["consecutive_failures"], 0)

        code, status, _ = self.run_scheduled(retryable=1)
        self.assertEqual(code, 1)
        self.assertEqual(status["result"]["retryable_failures"], 1)
        self.assertEqual(status["consecutive_failures"], 1)

    def test_timer_records_a_lock_timeout_as_deferral(self):
        code, status, delivered = self.run_scheduled(busy=True)
        self.assertEqual((code, delivered), (0, 0))
        self.assertIn("last_deferred_at", status)
        self.assertNotIn("last_success", status)
