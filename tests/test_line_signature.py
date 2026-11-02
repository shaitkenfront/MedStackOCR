from __future__ import annotations

import base64
import hashlib
import hmac
import unittest

from linebot.signature import verify_line_signature


class LineSignatureTest(unittest.TestCase):
    def test_verify_line_signature_success(self) -> None:
        secret = "test_secret"
        body = b'{"events":[]}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        self.assertTrue(verify_line_signature(secret, body, signature))

    def test_verify_line_signature_failure(self) -> None:
        self.assertFalse(verify_line_signature("a", b"{}", "invalid"))
        self.assertFalse(verify_line_signature("", b"{}", "invalid"))


if __name__ == "__main__":
    unittest.main()

