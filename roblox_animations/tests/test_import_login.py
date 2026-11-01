"""Importing never requires a login; missing downloads are reported instead.

Bundled R6 meshes, primitives and unions need no network. When Roblox-hosted
assets can't be downloaded (logged out, login unfinished, Online Access off),
the import still runs, the affected parts become placeholders, and the report
says why.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from ..core import auth
from ..operators import import_ops


class TestImportLogin(unittest.TestCase):
    def setUp(self):
        self.online = self.enterContext(
            mock.patch.object(auth, "is_online_access_allowed", return_value=True)
        )
        self.logging_in = self.enterContext(
            mock.patch.object(auth, "is_login_in_progress", return_value=False)
        )
        self.headers = self.enterContext(
            mock.patch.object(auth, "get_auth_headers", return_value={})
        )
        self.operator = SimpleNamespace(
            report=mock.Mock(),
            files=[],
            properties=SimpleNamespace(filepath="fixture.rbxm"),
            _import_one=mock.Mock(return_value="FINISHED"),
        )
        self.context = SimpleNamespace(
            window_manager=SimpleNamespace(fileselect_add=mock.Mock())
        )
        self.enterContext(
            mock.patch("roblox_animations.rig.filemesh.release_import_cache")
        )
        self.enterContext(
            mock.patch("roblox_animations.rig.textures.release_import_byte_cache")
        )

    def invoke(self):
        return import_ops.OBJECT_OT_ImportRbxm.invoke(self.operator, self.context, None)

    def execute(self):
        return import_ops.OBJECT_OT_ImportRbxm.execute(self.operator, self.context)

    def reported(self):
        return [call.args[1] for call in self.operator.report.call_args_list]

    def test_logged_out_can_still_open_file_browser(self):
        self.assertEqual(self.invoke(), {"RUNNING_MODAL"})
        self.context.window_manager.fileselect_add.assert_called_once_with(self.operator)

    def test_logged_out_import_runs_and_notes_the_login(self):
        self.assertEqual(self.execute(), {"FINISHED"})
        self.operator._import_one.assert_called_once_with(self.context, "fixture.rbxm")
        self.assertIn("Roblox Account", self.operator._login_note)

    def test_online_access_disabled_explains_required_setting(self):
        self.online.return_value = False
        self.assertEqual(self.execute(), {"FINISHED"})
        self.headers.assert_not_called()
        self.assertIn("Online Access", self.operator._login_note)

    def test_incomplete_login_is_noted(self):
        self.logging_in.return_value = True
        self.assertEqual(self.execute(), {"FINISHED"})
        self.headers.assert_not_called()
        self.assertIn("hadn't finished", self.operator._login_note)

    def test_valid_or_refreshed_login_allows_downloads(self):
        # get_auth_headers refreshes expired saved sessions. A stale local
        # is_logged_in flag must not reject a session that refreshes successfully.
        with mock.patch.object(auth, "is_logged_in", return_value=False):
            self.headers.return_value = {"Authorization": "Bearer test-only"}
            self.assertEqual(self.invoke(), {"RUNNING_MODAL"})
            self.assertEqual(self.execute(), {"FINISHED"})
            self.operator._import_one.assert_called_once_with(
                self.context, "fixture.rbxm"
            )
            self.assertIsNone(self.operator._login_note)
            self.assertEqual(self.reported(), ["Imported 1 model(s)."])

    def test_problems_are_reported_with_the_login_reason_first(self):
        def import_with_placeholder(_context, _path):
            self.operator._problems.append("'Sword' is a placeholder box: its mesh needs a Roblox login to download.")
            return "FINISHED"

        self.operator._import_one.side_effect = import_with_placeholder
        self.assertEqual(self.execute(), {"FINISHED"})
        messages = self.reported()
        self.assertIn("1 problem", messages[0].replace("2 problem", "1 problem"))
        self.assertIn("Roblox Account", messages[1])
        self.assertIn("placeholder box", messages[2])

    def test_nothing_imported_still_reports_why(self):
        self.operator._import_one.return_value = "CANCELLED"
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.assertIn("No models imported", self.reported()[0])

    def test_multi_file_import_imports_every_file(self):
        self.operator.files = [
            SimpleNamespace(name="one.rbxm"),
            SimpleNamespace(name="two.rbxl"),
        ]
        self.operator.directory = "fixtures"
        self.assertEqual(self.execute(), {"FINISHED"})
        self.assertEqual(self.operator._import_one.call_count, 2)


if __name__ == "__main__":
    unittest.main()
