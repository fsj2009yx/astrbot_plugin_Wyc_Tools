"""NapCat 群文件响应合同回归；仅使用标准库即可运行。"""

import unittest

from core.group_files import unwrap_group_file_response


class GroupFileResponseTests(unittest.TestCase):
    def test_unwrapped_payloads_are_preserved(self):
        payloads = (
            {},
            {"ok": True},
            {"files": [{"file_id": "file-id", "code": 123}], "folders": []},
            {"file_id": "uploaded-file-id"},
            {"folder_id": "folder-id", "folder_name": "资料"},
            {"retCode": 0, "retMsg": ""},
            {"result": 0, "errMsg": ""},
            {"result": "0"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIs(unwrap_group_file_response(payload), payload)

    def test_full_onebot_envelope_is_unwrapped(self):
        for status in ("ok", "OK", " ok "):
            for retcode in (0, "0"):
                with self.subTest(status=status, retcode=retcode):
                    payload = {"ok": True}
                    self.assertIs(
                        unwrap_group_file_response({"status": status, "retcode": retcode, "data": payload}),
                        payload,
                    )

    def test_legacy_empty_success(self):
        for raw in (None, {}, {"status": "ok", "retcode": 0, "data": None}):
            with self.subTest(raw=raw):
                self.assertEqual(unwrap_group_file_response(raw), {})

    def test_native_create_folder_result(self):
        payload = {
            "result": {"retCode": 0, "retMsg": ""},
            "groupItem": {"folderInfo": {"folderId": "/folder-id", "folderName": "资料"}},
        }
        self.assertIs(unwrap_group_file_response(payload), payload)
        wrapper = {"result": 0, "resultWithGroupItem": payload}
        self.assertIs(unwrap_group_file_response(wrapper), wrapper)

    def test_native_delete_transfer_and_move_results(self):
        for field in ("transGroupFileResult", "moveGroupFileResult"):
            with self.subTest(field=field):
                payload = {
                    "result": 0,
                    field: {"result": {"retCode": 0, "retMsg": ""}, "successFileIdList": ["file-id"], "failFileIdList": []},
                }
                self.assertIs(unwrap_group_file_response(payload), payload)
        folder_delete = {"result": 0, "groupFileCommonResult": {"retCode": 0, "retMsg": ""}}
        self.assertIs(unwrap_group_file_response(folder_delete), folder_delete)

    def test_onebot_failures_preserve_the_reason(self):
        cases = (
            {"status": "failed", "retcode": 500, "message": "没有权限", "data": None},
            {"status": "ok", "retcode": 1, "wording": "失败码1", "data": {}},
            {"status": "ok", "retcode": 100, "reason": "失败码100", "data": {}},
            {"status": "error", "retcode": 0, "errMsg": "接口错误", "data": {}},
        )
        for raw in cases:
            with self.subTest(raw=raw):
                reason = next(raw[key] for key in ("reason", "errMsg", "message", "wording") if key in raw)
                with self.assertRaisesRegex(ValueError, reason):
                    unwrap_group_file_response(raw)

    def test_async_is_not_reported_as_completed(self):
        for retcode in (0, 1):
            with self.subTest(retcode=retcode):
                with self.assertRaisesRegex(ValueError, "异步.*尚未完成"):
                    unwrap_group_file_response({"status": "async", "retcode": retcode, "data": {"ok": True}})

    def test_business_failures_preserve_the_reason(self):
        cases = (
            {"ok": False, "reason": "移动失败"},
            {"result": 5, "errMsg": "原生调用失败"},
            {"retCode": 7, "retMsg": "目录删除失败"},
            {"result": {"retCode": 8, "retMsg": "文件夹创建失败"}, "groupItem": {}},
            {"groupFileCommonResult": {"retCode": 9, "clientWording": "文件夹非空"}},
            {"transGroupFileResult": {"result": {"retCode": 10, "retMsg": "文件删除失败"}}},
            {"resultWithGroupItem": {"result": {"retCode": 11, "retMsg": "重复文件夹"}}},
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:
                    unwrap_group_file_response(raw)
                self.assertTrue(any(reason in str(error.exception) for reason in (
                    "移动失败", "原生调用失败", "目录删除失败", "文件夹创建失败", "文件夹非空", "文件删除失败", "重复文件夹",
                )))

    def test_partial_failure_lists_fail_even_when_codes_are_zero(self):
        for field in (None, "transGroupFileResult", "moveGroupFileResult"):
            payload = {"result": {"retCode": 0}, "failFileIdList": ["failed-file"]}
            raw = {field: payload} if field else payload
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "failFileIdList"):
                    unwrap_group_file_response(raw)

    def test_metadata_is_not_recursively_treated_as_a_result(self):
        payload = {
            "files": [{"file_id": "id", "code": 500, "retCode": 1, "result": 1}],
            "groupItem": {"folderInfo": {"result": 1, "retCode": 2}},
        }
        self.assertIs(unwrap_group_file_response(payload), payload)

    def test_payload_failures_keep_envelope_reason(self):
        with self.assertRaisesRegex(ValueError, "完整错误原因"):
            unwrap_group_file_response({"status": "ok", "retcode": 0, "message": "完整错误原因", "data": {"ok": False}})

    def test_invalid_response_types_are_rejected(self):
        for raw in (True, False, 0, 1, "", "ok", [], [1]):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    unwrap_group_file_response(raw)
        for data in (True, 0, "ok", []):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    unwrap_group_file_response({"status": "ok", "retcode": 0, "data": data})

    def test_invalid_codes_never_become_success(self):
        for value in (1, 100, -1, 0.5, True, False, None, "invalid", "0.5"):
            for field in ("retcode", "retCode", "result"):
                raw = {field: value}
                if field == "retcode":
                    raw.update(status="ok", data={})
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        unwrap_group_file_response(raw)


if __name__ == "__main__":
    unittest.main()
