import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from plowwhip.provider import (
    HostBridgeError,
    _bridge_configuration,
    _bridge_post,
    bridge_health,
)


class BridgeStatusTest(unittest.TestCase):
    def test_missing_token_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HostBridgeError) as raised:
                _bridge_configuration()
        self.assertEqual(raised.exception.failure_kind, "not_configured")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bridge_health(), "disabled")

    def test_network_failure_is_unreachable(self):
        with patch("plowwhip.provider.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(HostBridgeError) as raised:
                _bridge_post(
                    "http://localhost:8765", "token", "/v1/jobs/status", {}, 1
                )
        self.assertEqual(raised.exception.failure_kind, "unreachable")

    def test_health_keeps_authenticated_rejection_reachable(self):
        with patch.dict(
            os.environ, {"PLOW_WHIP_BRIDGE_TOKEN": "test-token"}, clear=True
        ), patch(
            "plowwhip.provider._bridge_post",
            side_effect=HostBridgeError("rejected", status=401),
        ):
            self.assertEqual(bridge_health(), "ok")


if __name__ == "__main__":
    unittest.main()
