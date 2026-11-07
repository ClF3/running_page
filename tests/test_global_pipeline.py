"""Website publication and international delivery fail independently."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_page.automation import __main__ as cli
from run_page.automation.common import read_json


class DeliveryPipelineTest(unittest.TestCase):
    def run_case(self, source_error=False, publish_error=False, global_error=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "state": str(root / "state"),
                        "source": str(root / "code"),
                        "global_sync": True,
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
