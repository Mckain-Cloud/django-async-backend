"""Runs the sample pytest project in tests/pytest_sample with a real pytest
process, so the plugin is tested the way users run it (entry point, pytest-
django test database, pytest-asyncio loops)."""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = TESTS_DIR / "pytest_sample"
EXPECTED_PASSED = 12


def _installed(name):
    return importlib.util.find_spec(name) is not None


@unittest.skipUnless(
    all(_installed(m) for m in ("pytest", "pytest_django", "pytest_asyncio")),
    "pytest, pytest-django and pytest-asyncio are required",
)
class PytestPluginTests(unittest.TestCase):
    def test_sample_project(self):
        env = dict(os.environ)
        env["DAB_BASE_SETTINGS"] = os.environ.get(
            "DJANGO_SETTINGS_MODULE", "settings"
        )
        env["DJANGO_SETTINGS_MODULE"] = "settings_pytest"
        env["PYTHONUTF8"] = "1"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(SAMPLE_DIR),
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "--rootdir",
            str(SAMPLE_DIR),
        ]
        proc = subprocess.run(
            cmd, cwd=TESTS_DIR, capture_output=True, text=True, env=env
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("%d passed" % EXPECTED_PASSED, output)
