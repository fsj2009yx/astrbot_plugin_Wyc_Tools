"""Exercise the real group-file methods without requiring an AstrBot installation.

Only the selected methods and their local helpers are loaded from main.py. API
calls use AsyncMock, so these tests never alter a real group's files.
"""

import ast
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import typing
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

from core import group_files


ROOT = Path(__file__).resolve().parents[1]
GROUP_ID = "123456789"
FILE_ID = "87654321-1234-4abc-9000-123456789012"
FOLDER_ID = "/12345678"


class MessageChain:
    def message(self, text):
        self.text = text
        return self


def load_plugin_methods():
    """Keep production method bodies, excluding framework initialization/hooks."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Main")
    methods = {
        node.name: node
        for node in plugin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    selected = {
        "list_group_files", "delete_group_file", "upload_group_file",
        "create_group_file_folder", "delete_group_folder", "move_group_file",
        "rename_group_file", "trans_group_file", "get_group_file_url",
        "cmd_rename_group_file", "_privacy_filter_result",
    }
    pending = list(selected)
    while pending:
        for node in ast.walk(methods[pending.pop()]):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                dependency = node.attr
                if dependency in methods and dependency != "_get_client" and dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    body = []
    for node in plugin.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            body.append(copy.deepcopy(node))
    for name in sorted(selected):
        method = copy.deepcopy(methods[name])
        method.decorator_list = [
            decorator for decorator in method.decorator_list
            if isinstance(decorator, ast.Name) and decorator.id in ("staticmethod", "classmethod")
        ]
        body.append(method)
    safe_error = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_safe_error_msg"))
    isolated = ast.Module(body=[safe_error, ast.ClassDef(name="Plugin", bases=[], keywords=[], body=body, decorator_list=[])], type_ignores=[])
    namespace = {
        **vars(typing),
        **{name: value for name, value in vars(group_files).items() if not name.startswith("__")},
        "asyncio": asyncio, "datetime": datetime, "timedelta": timedelta,
        "timezone": timezone, "hashlib": hashlib, "json": json,
        "os": os, "re": re, "Path": Path, "MessageChain": MessageChain,
        "AiocqhttpMessageEvent": object, "AstrMessageEvent": object,
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                namespace[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    exec(compile(ast.fix_missing_locations(isolated), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Plugin"]


class GroupFileToolsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.Plugin = load_plugin_methods()

    def setUp(self):
        self.plugin = self.Plugin()
        self.plugin.config = {"group_manage_enabled": True, "client_timeout": 1}
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={"ok": True}))
        self.event = SimpleNamespace(
            bot=self.bot, get_group_id=lambda: GROUP_ID,
            send=AsyncMock(), message_str="",
        )
        self.plugin._get_client = AsyncMock(return_value=self.bot)

    def assert_action(self, action, **expected):
        self.bot.call_action.assert_awaited_once()
        args, kwargs = self.bot.call_action.await_args
        kwargs = dict(kwargs)
        self.assertEqual(args, (action,))
        self.assertEqual(str(kwargs.pop("group_id")), GROUP_ID)
        for key, value in expected.items():
            self.assertEqual(kwargs[key], value)
        return kwargs

    async def test_root_listing_includes_operable_ids_dates_and_all_returned_files(self):
        self.bot.call_action.return_value = {
            "files": [
                {"file_id": f"file-{index}", "file_name": f"file-{index}.txt", "file_size": 1024,
                 "busid": 104, "upload_time": 1700000000}
                for index in range(31)
            ],
            "folders": [{"folder_id": FOLDER_ID, "folder_name": "资料", "total_file_count": 3}],
        }
        result = await self.plugin.list_group_files(self.event, file_count=71)
        self.assertEqual(result["status"], "success")
        message = result["message"]
        for expected in ("file-30.txt", "资料", FOLDER_ID, "104", "2023-11-14", "current_parent_directory", "/"):
            self.assertIn(expected, message)
        self.assertNotIn("前20", message)
        self.assert_action("get_group_root_files", file_count=71)

    async def test_subfolder_listing_uses_folder_endpoint_and_parent_id(self):
        self.bot.call_action.return_value = {"status": "ok", "retcode": 0, "data": {"files": [], "folders": []}}
        result = await self.plugin.list_group_files(self.event, folder_id=FOLDER_ID, file_count=80)
        self.assertEqual(result["status"], "success")
        self.assertIn(FOLDER_ID, result["message"])
        self.assert_action("get_group_files_by_folder", folder_id=FOLDER_ID, file_count=80)

    async def test_folder_only_listing_does_not_report_empty_directory(self):
        self.bot.call_action.return_value = {"folders": [{"folder_id": FOLDER_ID, "folder_name": "资料"}], "files": None}
        result = await self.plugin.list_group_files(self.event)
        self.assertEqual(result["status"], "success")
        self.assertIn("资料", result["message"])
        self.assertNotIn("没有文件或文件夹", result["message"])

    async def test_listing_does_not_turn_failed_api_response_into_empty_directory(self):
        self.bot.call_action.return_value = {"status": "failed", "retcode": 500, "message": "permission denied"}
        result = await self.plugin.list_group_files(self.event)
        self.assertEqual(result["status"], "error")
        self.assertIn("permission denied", result["message"])

    async def test_move_accepts_unwrapped_and_full_response_and_uses_documented_names(self):
        for response in ({"ok": True}, {"status": "ok", "retcode": 0, "data": {"ok": True}}):
            with self.subTest(response=response):
                self.bot.call_action.reset_mock()
                self.bot.call_action.return_value = response
                result = await self.plugin.move_group_file(self.event, FILE_ID, "/", FOLDER_ID)
                self.assertEqual(result["status"], "success")
                self.assert_action("move_group_file", file_id=FILE_ID, current_parent_directory="/", target_parent_directory=FOLDER_ID)

    async def test_rename_preserves_spaces_in_new_filename(self):
        result = await self.plugin.rename_group_file(self.event, FILE_ID, FOLDER_ID, "会议 记录 2026.txt")
        self.assertEqual(result["status"], "success")
        self.assert_action("rename_group_file", file_id=FILE_ID, current_parent_directory=FOLDER_ID, new_name="会议 记录 2026.txt")

    async def test_rename_command_preserves_spaces(self):
        self.event.message_str = f"/rename_group_file {FILE_ID} {FOLDER_ID} 会议 记录 2026.txt"
        self.plugin.rename_group_file = AsyncMock(return_value={"status": "success", "message": "renamed"})
        await self.plugin.cmd_rename_group_file(self.event)
        self.plugin.rename_group_file.assert_awaited_once_with(self.event, FILE_ID, FOLDER_ID, "会议 记录 2026.txt")
        self.event.send.assert_awaited_once()

    async def test_delete_forwards_optional_busid_without_hardcoding(self):
        self.bot.call_action.return_value = {"retCode": 0, "retMsg": ""}
        result = await self.plugin.delete_group_file(self.event, FILE_ID, busid=104)
        self.assertEqual(result["status"], "success")
        kwargs = self.assert_action("delete_group_file", file_id=FILE_ID)
        self.assertEqual(kwargs.get("busid", kwargs.get("bus_id")), 104)

    async def test_delete_does_not_assume_business_id_when_omitted(self):
        result = await self.plugin.delete_group_file(self.event, FILE_ID)
        self.assertEqual(result["status"], "success")
        kwargs = self.assert_action("delete_group_file", file_id=FILE_ID)
        self.assertNotIn("busid", kwargs)
        self.assertNotIn("bus_id", kwargs)

    async def test_create_extracts_folder_name_and_id_from_native_group_item(self):
        for group_item in (
            {"folderInfo": {"folderId": FOLDER_ID, "folderName": "新 资料"}},
            [{"folderInfo": {"folderId": FOLDER_ID, "folderName": "新 资料"}}],
        ):
            with self.subTest(group_item=group_item):
                self.bot.call_action.reset_mock()
                self.bot.call_action.return_value = {"result": {"retCode": 0}, "groupItem": group_item}
                result = await self.plugin.create_group_file_folder(self.event, "新 资料")
                self.assertEqual(result["status"], "success")
                self.assertIn(FOLDER_ID, result["message"])
                self.assertIn("新 资料", result["message"])
                kwargs = self.assert_action("create_group_file_folder")
                self.assertEqual(kwargs.get("folder_name", kwargs.get("name")), "新 资料")

    async def test_upload_to_subfolder_uses_folder_field(self):
        self.bot.call_action.return_value = {"status": "ok", "retcode": 0, "data": {"file_id": "uploaded-id"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "source.txt"
            file_path.write_text("test", encoding="utf-8")
            result = await self.plugin.upload_group_file(self.event, str(file_path), "新 文件.txt", folder_id=FOLDER_ID)
        self.assertEqual(result["status"], "success")
        self.assert_action("upload_group_file", file=str(file_path), name="新 文件.txt", folder=FOLDER_ID)

    async def test_upload_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "source.txt"
            file_path.write_text("test", encoding="utf-8")
            for response in (
                {"status": "failed", "retcode": 500, "message": "denied"},
                {"retCode": 5, "retMsg": "denied"},
            ):
                with self.subTest(response=response):
                    self.bot.call_action.return_value = response
                    result = await self.plugin.upload_group_file(self.event, str(file_path))
                    self.assertEqual(result["status"], "error")
                    self.assertIn("denied", result["message"])

    async def test_download_response_without_url_is_error(self):
        self.bot.call_action.return_value = {}
        result = await self.plugin.get_group_file_url(self.event, FILE_ID)
        self.assertEqual(result["status"], "error")

    async def test_download_link_uses_url_endpoint_and_returns_url(self):
        url = "https://example.com/download/file"
        self.bot.call_action.return_value = {"status": "ok", "retcode": 0, "data": {"url": url}}
        result = await self.plugin.get_group_file_url(self.event, FILE_ID)
        self.assertEqual(result["status"], "success")
        self.assertTrue(url in result.get("message", "") or result.get("url") == url)
        self.assert_action("get_group_file_url", file_id=FILE_ID)

    async def test_trans_uses_transfer_endpoint_and_accepts_unwrapped_success(self):
        result = await self.plugin.trans_group_file(self.event, FILE_ID)
        self.assertEqual(result["status"], "success")
        self.assert_action("trans_group_file", file_id=FILE_ID)

    async def test_mutations_do_not_report_native_failures_as_success(self):
        calls = (
            ("delete_group_file", (FILE_ID,)),
            ("delete_group_folder", (FOLDER_ID,)),
            ("create_group_file_folder", ("资料",)),
            ("move_group_file", (FILE_ID, "/", FOLDER_ID)),
            ("rename_group_file", (FILE_ID, FOLDER_ID, "renamed.txt")),
            ("trans_group_file", (FILE_ID,)),
        )
        for name, args in calls:
            for response in ({"ok": False, "message": "denied"}, {"retCode": 5, "retMsg": "denied"}):
                with self.subTest(method=name, response=response):
                    self.bot.call_action.return_value = response
                    result = await getattr(self.plugin, name)(self.event, *args)
                    self.assertEqual(result["status"], "error")
                    self.assertIn("denied", result["message"])

    async def test_empty_required_values_and_root_folder_deletion_do_not_call_api(self):
        calls = (
            ("delete_group_file", ("   ",)),
            ("delete_group_folder", ("/",)),
            ("delete_group_folder", ("   ",)),
            ("create_group_file_folder", ("   ",)),
            ("move_group_file", (FILE_ID, "/", "   ")),
            ("rename_group_file", (FILE_ID, FOLDER_ID, "   ")),
            ("trans_group_file", ("   ",)),
            ("get_group_file_url", ("   ",)),
        )
        for name, args in calls:
            with self.subTest(method=name, args=args):
                result = await getattr(self.plugin, name)(self.event, *args)
                self.assertEqual(result["status"], "error")
        self.bot.call_action.assert_not_awaited()

    async def test_disabled_group_management_never_calls_api(self):
        self.plugin.config["group_manage_enabled"] = False
        result = await self.plugin.move_group_file(self.event, FILE_ID, "/", FOLDER_ID)
        self.assertEqual(result["status"], "error")
        self.bot.call_action.assert_not_awaited()

    async def test_missing_group_id_never_calls_api(self):
        self.event.get_group_id = lambda: ""
        result = await self.plugin.delete_group_file(self.event, FILE_ID)
        self.assertEqual(result["status"], "error")
        self.bot.call_action.assert_not_awaited()

    async def test_api_exception_is_returned_as_error(self):
        self.bot.call_action.side_effect = RuntimeError("connection closed")
        result = await self.plugin.rename_group_file(self.event, FILE_ID, "/", "renamed.txt")
        self.assertEqual(result["status"], "error")
        self.assertIn("connection closed", result["message"])

    async def test_privacy_preserves_group_file_metadata_but_still_masks_people(self):
        self.plugin.config["privacy_mode"] = "privacy"
        url = "https://example.com/123456789/file?signature=987654321012&expiry=1700000000"
        message = (
            f"群 {GROUP_ID}\n创建者 QQ: {GROUP_ID}\n"
            f"  file_id: {GROUP_ID}\n"
            f"  file_id: {FILE_ID}\n"
            f"  folder_id: {FOLDER_ID}\n"
            f"  current_parent_directory: {FOLDER_ID}\n"
            f"  target_parent_directory: /\n"
            f"  busid: 104\n"
            f"  url: {url}\n"
            "  上传时间: 2023-11-14 22:13:20 UTC [upload_time: 1700000000]"
        )
        original = {"status": "success", "message": message}
        result = self.plugin._privacy_filter_result(original, tool_name="list_group_files")
        self.assertEqual(original["message"], message)
        self.assertNotIn(f"群 {GROUP_ID}", result["message"])
        self.assertNotIn(f"创建者 QQ: {GROUP_ID}", result["message"])
        for expected in (
            f"file_id: {GROUP_ID}", f"file_id: {FILE_ID}", f"folder_id: {FOLDER_ID}",
            f"current_parent_directory: {FOLDER_ID}", "target_parent_directory: /",
            "busid: 104", f"url: {url}", "2023-11-14 22:13:20 UTC",
        ):
            self.assertIn(expected, result["message"])

    async def test_other_tools_cannot_bypass_privacy_with_metadata_like_lines(self):
        self.plugin.config["privacy_mode"] = "privacy"
        message = f"file_id: {GROUP_ID}\nfolder_id: {FOLDER_ID}\nurl: https://example.com/{GROUP_ID}"
        result = self.plugin._privacy_filter_result({"message": message}, tool_name="search_contacts")
        self.assertNotIn(GROUP_ID, result["message"])
        self.assertNotIn(FOLDER_ID, result["message"])


if __name__ == "__main__":
    unittest.main()
