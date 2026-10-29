"""Even accidentally unmocked API clients cannot connect through a local proxy."""
from pathlib import Path
import runpy
import socket
import unittest


offline_network = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/test-offline.py'))['offline_network']


class OfflineNetworkTests(unittest.TestCase):
    def test_blocks_inet_connections_including_local_proxy_and_dns(self):
        with socket.socket() as stream, offline_network():
            for operation in (stream.connect, stream.connect_ex):
                with self.assertRaisesRegex(AssertionError, 'offline_network_forbidden'):
                    operation(('127.0.0.1', 10808))
            with self.assertRaisesRegex(AssertionError, 'offline_network_forbidden'):
                socket.getaddrinfo('example.invalid', 443)
