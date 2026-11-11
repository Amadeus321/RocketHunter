"""Install and verify the process-wide unittest network deny guard."""
from __future__ import annotations

import socket
import subprocess
import sys
import unittest
import urllib.request

from network_guard import NetworkAccessBlocked, install_network_guard

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
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import urllib.request; urllib.request.urlopen('https://provider.invalid')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network access blocked by unittest guard", result.stderr)


if __name__ == "__main__":
    unittest.main()
