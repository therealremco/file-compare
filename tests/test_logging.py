import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_compare
import review
from file_compare import SSH_LOG


class CommandLoggingTests(unittest.TestCase):
    def test_comparison_and_review_logging_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            for configured, flags, expected in (
                (None, [], False), (True, [], True),
                (False, ["--debug-ssh"], True),
                (True, ["--no-debug-ssh"], False),
            ):
                values = dict(host="server", remote_path="/remote", local_path=str(root))
                if configured is not None:
                    values["debug_ssh"] = configured
                config.write_text(json.dumps(values))
                args = ["--config", str(config), *flags]

                def log_stage(stage):
                    SSH_LOG.debug("%s debug detail", stage)
                    SSH_LOG.info("%s progress", stage)
                    SSH_LOG.warning("%s warning", stage)
                    SSH_LOG.error("%s error", stage)

                def compare(options):
                    log_stage("compare")
                    return 1, 0

                def prepare(*args):
                    log_stage("import")

                def serve():
                    # The logger must remain configured for later request threads.
                    log_stage("download")
                    raise KeyboardInterrupt

                previous = (SSH_LOG.level, SSH_LOG.propagate, SSH_LOG.handlers.copy())
                for command in ("compare", "review"):
                    stderr = io.StringIO()
                    with self.subTest(command=command, configured=configured, flags=flags):
                        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                            if command == "compare":
                                with patch("file_compare.compare", side_effect=compare):
                                    code = file_compare.main(args)
                                stages = ["compare"]
                            else:
                                with patch("review.prepare", side_effect=prepare), patch(
                                    "review.ThreadingHTTPServer",
                                ) as server:
                                    server.return_value.__enter__.return_value.serve_forever.side_effect = serve
                                    code = review.main([*args, "--review-dir", str(root / "state")])
                                stages = ["import", "download"]
                        self.assertEqual(code, 0)
                        for stage in stages:
                            self.assertEqual(f"DEBUG SSH: {stage} debug detail" in stderr.getvalue(), expected)
                            for severity in ("progress", "warning", "error"):
                                self.assertIn(f"{stage} {severity}", stderr.getvalue())
                        self.assertEqual(
                            (SSH_LOG.level, SSH_LOG.propagate, SSH_LOG.handlers), previous,
                        )
