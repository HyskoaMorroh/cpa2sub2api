"""Management diagnostics must not infer a 403 cause or expose response bodies."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tool


class AdminDiagnosticsTests(unittest.TestCase):
    def test_403_is_neutral_private_and_not_retried(self):
        api = tool.Sub2Api({
            "sub2api_base_url": "https://fixture.invalid",
            "sub2api_admin_key": "admin-synthetic-fixture",
        })
        bodies = (
            '{"error":"permission_denied","private":"synthetic-private-marker"}',
            '<html>synthetic-private-marker: edge rejection</html>',
            '',
        )
        for body in bodies:
            with self.subTest(body_type="empty" if not body else body[0]):
                with patch.object(api, 'list_groups', side_effect=tool.ApiError(
                        403, body, '/api/v1/admin/groups/all')) as request:
                    ok, message = api.ping()
                request.assert_called_once_with()
                self.assertFalse(ok)
                self.assertIn('403', message)
                self.assertIn('原因尚未确认', message)
                self.assertNotIn('密钥权限不足', message)
                self.assertNotIn('synthetic-private-marker', message)
                self.assertNotIn('admin-synthetic-fixture', message)


if __name__ == '__main__':
    unittest.main()
