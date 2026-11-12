"""Install and verify the process-wide unittest network deny guard."""
from __future__ import annotations

import socket
import subprocess
import sys
import unittest
import urllib.request

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

try:
    from network_guard import NetworkAccessBlocked, install_network_guard
except ImportError:
    from tests.network_guard import NetworkAccessBlocked, install_network_guard

install_network_guard()


class NetworkGuardTests(unittest.TestCase):
    def test_unmocked_urllib_request_is_blocked_before_network_access(self):
        with self.assertRaisesRegex(NetworkAccessBlocked, "urllib.request.urlopen"):
            urllib.request.urlopen("https://provider.invalid")

    def test_unmocked_socket_connection_is_blocked_before_network_access(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaisesRegex(NetworkAccessBlocked, "socket.socket.connect"):
                sock.connect(("203.0.113.1", 443))
        finally:
            sock.close()

    def test_python_subprocess_inherits_network_guard(self):
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(TESTS_DIR)!r}); "
            "import network_guard; "
            "network_guard.install_network_guard(); "
            "import urllib.request; "
            "urllib.request.urlopen('https://provider.invalid')"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network access blocked by unittest guard", result.stderr)


if __name__ == "__main__":
    unittest.main()
