import subprocess
import sys
from pathlib import Path


def test_cloud_dispatcher_private_callbacks_and_file_outbox():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("cloud_flow_check.py"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
