"""群文件 API 返回体的兼容处理，不依赖 AstrBot 或 aiocqhttp。"""

from __future__ import annotations

from typing import Any


# QQ 原生回调及不同 NapCat 版本使用的业务结果节点。
# 不遍历 files/groupItem 等元数据，避免把文件自身的字段当作错误码。
_RESULT_FIELDS = (
    "result",
    "resultWithGroupItem",
    "groupFileCommonResult",
    "transGroupFileResult",
    "moveGroupFileResult",
    "renameGroupFileResult",
    "deleteGroupFileResult",
)
_REASON_FIELDS = (
    "reason", "retMsg", "errMsg", "message", "wording", "clientWording",
)


def _reason(result: dict[str, Any]) -> str:
    for key in _REASON_FIELDS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in _RESULT_FIELDS:
        value = result.get(key)
        if isinstance(value, dict):
            reason = _reason(value)
            if reason:
                return reason
    return ""


def _fail(result: dict[str, Any], detail: str, inherited_reason: str = "") -> None:
    reason = _reason(result) or inherited_reason
    raise ValueError(f"{reason}（{detail}）" if reason else detail)


def _is_zero(value: Any) -> bool:
    # 不用 int(value)：int(0.5) == 0 会把不合法的错误码当成成功。
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    return isinstance(value, str) and value.strip() == "0"


def _check_payload(result: dict[str, Any], inherited_reason: str = "") -> None:
    reason = _reason(result) or inherited_reason
    if "ok" in result and result["ok"] is not True:
        _fail(result, f"ok={result['ok']!r}", reason)
    if "retCode" in result and not _is_zero(result["retCode"]):
        _fail(result, f"retCode={result['retCode']!r}", reason)
    if result.get("failFileIdList"):
        _fail(result, "部分或全部文件操作失败（failFileIdList 非空）", reason)
    for key in _RESULT_FIELDS:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, dict):
            _check_payload(value, reason)
        elif key == "result":
            if not _is_zero(value):
                _fail(result, f"result={value!r}", reason)
        elif value is not None:
            _fail(result, f"{key} 返回格式无效", reason)


def unwrap_group_file_response(raw: Any) -> dict[str, Any]:
    """校验群文件操作并返回 payload，失败或异步未完成时抛出 ValueError。

    aiocqhttp 通常已经剥离 OneBot 的 status/retcode/data 信封，但兼容
    直接返回完整信封的客户端。部分旧适配器在写操作成功时返回 None。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("群文件接口返回格式无效：应为对象")

    payload = raw
    if "status" in raw or "retcode" in raw:
        status = raw.get("status")
        if status is not None:
            status = str(status).strip().lower()
            if status == "async":
                _fail(raw, "接口请求仍在异步处理中，尚未完成")
            if status != "ok":
                _fail(raw, f"status={status!r}")
        if "retcode" in raw and not _is_zero(raw["retcode"]):
            _fail(raw, f"retcode={raw['retcode']!r}")
        payload = raw.get("data")
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            _fail(raw, "群文件接口 data 返回格式无效：应为对象")

    _check_payload(payload, _reason(raw))
    return payload
