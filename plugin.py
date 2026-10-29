"""苏醒吧麦麦！（github.natsuyutori.wake-up-maimai）

行为约定：
- 默认（群不在白名单）：群内**非 @** 消息在入库与入站之前被丢弃，机器人不会看到；
  被 @ 时把该群最近 N 条消息注入 Maisaka 上下文，并追加强制回复指令后放行由 Maisaka 正常回复；
  若 ``reply.fallback_seconds`` 内该群仍没有发出任何消息，插件调用 Replyer 生成一条并补发。
- 白名单群：插件对入站消息完全不做处理，Maisaka 走原生逻辑（含主动回复）。
- 白名单可在群内用 @麦麦 /on、/off 切换，也可在 WebUI 页面增删，两者共享同一份数据；
  谁能执行命令由 ``permission.mode``（all / admin）与 ``permission.admins`` 决定。

实现要点：
- 丢弃只能发生在 Hook 阶段：abort 发生在 ``chat_manager.register_message`` 与文件入库之前，
  因此被丢弃的消息不会进入宿主数据库，最近 N 条上下文改由本插件自建环形缓存提供。
- 命令不走 ``@Command``：宿主用 ``processed_plain_text`` 做 ``search`` 匹配，而 @ 段会被渲染成
  ``@名片`` 前缀，``^/on$`` 匹配不到 ``@麦麦 /on``，因此在 Hook 内自行解析。
- 群会话 ``session_id`` 只由 platform/account_id/scope/group_id 决定，与发送者无关，因此可以先用
  ``chat.open_session`` 建出与后续消息完全相同的会话，再向其中注入上下文或发送回执。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

try:  # pydantic 2 是 SDK 的硬依赖，这里只是防御性兜底
    from pydantic import model_validator
except ImportError:  # pragma: no cover - 理论上不会发生
    model_validator = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLUGIN_ID = "github.natsuyutori.wake-up-maimai"
STATE_VERSION = 1
WHITELIST_FILENAME = "whitelist.json"
CONTEXT_CACHE_FILENAME = "context_cache.json"

# 注入内容默认前缀；支持 {count}（条数）与 {omitted}（省略提示）两个占位符
DEFAULT_CONTEXT_PREFIX = "【群聊最近 {count} 条消息（插件缓存，按时间正序）】{omitted}"
# 默认的强制回复指令，追加在注入内容末尾
DEFAULT_FORCE_NOTE = (
    "（上面是最近的群聊记录。这条消息明确 @ 了你：本轮必须调用 reply 工具直接回应，"
    "不允许沉默、不允许只思考不回复。）"
)

# QQ 群号：5-12 位数字，且不以 0 开头
_GROUP_ID_RE = re.compile(r"^[1-9]\d{4,11}$")
# 宿主的 processed_plain_text 会把 @ 段渲染成「@名片」，命令解析前先剥掉前导 @ 片段
_AT_PREFIX_RE = re.compile(r"^(?:\s*@[^\s@]+)+\s*")

# 入站判定结果
PASS = "pass"        # 完全放行，插件不干预该消息
DROP = "drop"        # 丢弃：非 @ 消息（不入库、不入站）
AT_PASS = "at_pass"  # 被 @：注入最近上下文后放行
CMD_ON = "cmd_on"    # 群内 /on
CMD_OFF = "cmd_off"  # 群内 /off

# 非文本段在缓存里的占位符
_SEGMENT_PLACEHOLDERS: Dict[str, str] = {
    "image": "[图片]",
    "emoji": "[表情]",
    "mface": "[表情]",
    "face": "[表情]",
    "voice": "[语音]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
}


# ======================================================================
# 纯函数：消息解析与判定（不依赖宿主，便于单元测试）
# ======================================================================


def iter_segments(segments: Any) -> Iterable[Dict[str, Any]]:
    """深度遍历消息段，展开 ``seglist`` 之类的嵌套结构。

    Args:
        segments: 原始分段列表。

    Yields:
        Dict[str, Any]: 单个消息段。
    """

    if not isinstance(segments, list):
        return
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "seglist":
            yield from iter_segments(segment.get("data"))
            continue
        yield segment


def render_message_text(message: Dict[str, Any]) -> str:
    """把消息段渲染成一行可读文本，非文本段替换为占位符。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        str: 渲染结果；没有可读内容时返回空字符串。
    """

    parts: List[str] = []
    for segment in iter_segments(message.get("raw_message")):
        segment_type = str(segment.get("type") or "")
        data = segment.get("data")
        if segment_type == "text":
            text = data if isinstance(data, str) else ""
            if text.strip():
                parts.append(text.strip())
            continue
        placeholder = _SEGMENT_PLACEHOLDERS.get(segment_type)
        if placeholder:
            parts.append(placeholder)
    return " ".join(parts).strip()


def strip_at_prefix(text: str) -> str:
    """剥掉文本开头的 @ 片段（宿主的 @名片 渲染与用户手打的 @ 都覆盖）。

    Args:
        text: 原始文本。

    Returns:
        str: 去除前导 @ 片段后的文本。
    """

    if not text:
        return ""
    # 用户手打的 @ 可能是全角 ＠，先归一化再剥离
    normalized = unicodedata.normalize("NFKC", text)
    return _AT_PREFIX_RE.sub("", normalized, count=1).strip()


def normalize_command_text(text: str) -> str:
    """命令比对用的归一化：全角转半角、去首尾空白、大小写折叠。

    Args:
        text: 原始文本。

    Returns:
        str: 归一化后的文本。
    """

    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).strip()
    return normalized.casefold()


def parse_toggle(text: str, on_words: Sequence[str], off_words: Sequence[str]) -> Optional[str]:
    """判断文本是否为白名单开关命令。

    使用整串相等比较，避免 ``/once``、``/oncall`` 之类的误命中。

    Args:
        text: 已剥掉 @ 前缀的文本。
        on_words: 加入白名单的命令词。
        off_words: 移出白名单的命令词。

    Returns:
        Optional[str]: ``"on"`` 或 ``"off"``；不是命令时返回 ``None``。
    """

    candidate = normalize_command_text(text)
    if not candidate:
        return None
    for word in on_words or ():
        if word and candidate == normalize_command_text(word):
            return "on"
    for word in off_words or ():
        if word and candidate == normalize_command_text(word):
            return "off"
    return None


def group_id_of(message: Dict[str, Any]) -> str:
    """读取群号；私聊返回空字符串。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        str: 群号。
    """

    info = message.get("message_info")
    if not isinstance(info, dict):
        return ""
    group = info.get("group_info")
    if not isinstance(group, dict):
        return ""
    group_id = group.get("group_id")
    return str(group_id).strip() if group_id is not None else ""


def group_name_of(message: Dict[str, Any]) -> str:
    """读取群名（可能为空）。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        str: 群名。
    """

    info = message.get("message_info")
    if not isinstance(info, dict):
        return ""
    group = info.get("group_info")
    if not isinstance(group, dict):
        return ""
    return str(group.get("group_name") or "").strip()[:64]


def user_of(message: Dict[str, Any]) -> Tuple[str, str]:
    """读取发送者 QQ 号与展示名。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        Tuple[str, str]: ``(user_id, nickname)``。
    """

    info = message.get("message_info")
    if not isinstance(info, dict):
        return "", ""
    user = info.get("user_info")
    if not isinstance(user, dict):
        return "", ""
    user_id = str(user.get("user_id") or "").strip()
    nickname = str(user.get("user_cardname") or user.get("user_nickname") or "").strip()
    return user_id, nickname


def timestamp_of(message: Dict[str, Any]) -> float:
    """读取消息时间戳，失败时退回当前时间。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        float: Unix 时间戳（秒）。
    """

    raw = message.get("timestamp")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return time.time()
    if value <= 0:
        return time.time()
    # 兼容毫秒时间戳
    return value / 1000.0 if value > 1e11 else value


def bot_accounts(message: Dict[str, Any]) -> Set[str]:
    """推断当前机器人的平台账号 ID 集合。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        Set[str]: 可能的机器人账号 ID。
    """

    accounts: Set[str] = set()
    account_id = message.get("account_id")
    if account_id is not None and str(account_id).strip():
        accounts.add(str(account_id).strip())
    info = message.get("message_info")
    additional = info.get("additional_config") if isinstance(info, dict) else None
    if isinstance(additional, dict):
        for key in ("platform_io_account_id", "self_id", "bot_id", "qq_account"):
            value = additional.get(key)
            if value is not None and str(value).strip():
                accounts.add(str(value).strip())
    return accounts


def at_targets(message: Dict[str, Any]) -> List[str]:
    """收集消息里所有 @ 的目标账号。

    Args:
        message: Hook 载荷中的消息字典。

    Returns:
        List[str]: 被 @ 的账号 ID 列表。
    """

    targets: List[str] = []
    for segment in iter_segments(message.get("raw_message")):
        if str(segment.get("type") or "") != "at":
            continue
        data = segment.get("data")
        target: Any = None
        if isinstance(data, dict):
            for key in ("target_user_id", "qq", "user_id", "target"):
                value = data.get(key)
                if value is not None and str(value).strip():
                    target = value
                    break
        elif isinstance(data, str):
            target = data
        if target is not None and str(target).strip():
            targets.append(str(target).strip())
    return targets


def detect_at(message: Dict[str, Any], unresolved_policy: str = "drop") -> Tuple[bool, bool]:
    """判断消息是否 @ 了机器人，以及机器人身份是否可确认。

    优先信任适配器给出的 ``is_at``，其次用 @ 段目标与机器人账号比对。
    适配器没有提供机器人账号时，按 ``unresolved_policy`` 处理。

    Args:
        message: Hook 载荷中的消息字典。
        unresolved_policy: ``drop`` 表示按非 @ 处理，``pass`` 表示放行。

    Returns:
        Tuple[bool, bool]: ``(是否 @ 机器人, 身份是否已确认)``。
    """

    if message.get("is_at") is True:
        return True, True
    targets = at_targets(message)
    accounts = bot_accounts(message)
    if accounts:
        return any(target in accounts for target in targets), True
    if targets:
        return (str(unresolved_policy).strip().lower() == "pass"), False
    return False, True


@dataclass(frozen=True)
class Decision:
    """单条入站消息的处理决策。"""

    action: str
    identity_resolved: bool = True


def decide_message(
    message: Dict[str, Any],
    *,
    whitelisted: bool,
    enabled: bool,
    on_words: Sequence[str],
    off_words: Sequence[str],
    unresolved_policy: str = "drop",
) -> Decision:
    """根据消息内容与群状态给出处理决策。

    顺序很关键：命令判定必须在白名单判定之前，否则白名单群里的 ``@麦麦 /off``
    永远无法把群移出白名单。

    Args:
        message: Hook 载荷中的消息字典。
        whitelisted: 该群是否在白名单中。
        enabled: 插件总开关。
        on_words: 加入白名单的命令词。
        off_words: 移出白名单的命令词。
        unresolved_policy: 机器人身份无法确认时的处理策略。

    Returns:
        Decision: 处理决策。
    """

    if not enabled or not isinstance(message, dict):
        return Decision(PASS)
    if message.get("is_notify"):
        return Decision(PASS)
    group_id = group_id_of(message)
    if not group_id:
        # 私聊不归本插件管
        return Decision(PASS)

    is_at, identity_resolved = detect_at(message, unresolved_policy)
    if not is_at:
        return Decision(PASS if whitelisted else DROP, identity_resolved=identity_resolved)

    text = strip_at_prefix(render_message_text(message))
    toggle = parse_toggle(text, on_words, off_words)
    if toggle == "on":
        return Decision(CMD_ON, identity_resolved=identity_resolved)
    if toggle == "off":
        return Decision(CMD_OFF, identity_resolved=identity_resolved)
    if whitelisted:
        return Decision(PASS, identity_resolved=identity_resolved)
    return Decision(AT_PASS, identity_resolved=identity_resolved)


def format_context_prefix(template: str, count: int, omitted: str = "") -> str:
    """按占位符渲染注入前缀，模板本身有问题时退回默认前缀。

    Args:
        template: 含 ``{count}`` / ``{omitted}`` 占位符的模板。
        count: 本次渲染的条数。
        omitted: 省略提示文本。

    Returns:
        str: 渲染后的前缀文本。
    """

    text = (template or "").strip() or DEFAULT_CONTEXT_PREFIX
    try:
        return text.format(count=count, omitted=omitted)
    except (KeyError, IndexError, ValueError):
        # 用户模板里有多余的 { 或未知占位符：退化为纯文本替换，不能因为配置写错就崩
        return text.replace("{count}", str(count)).replace("{omitted}", omitted)


def render_context(
    entries: Sequence["ContextEntry"],
    *,
    prefix: str = DEFAULT_CONTEXT_PREFIX,
    note: str = "",
    max_total_chars: int = 4000,
    message_max_chars: int = 300,
) -> str:
    """把缓存消息渲染成注入给 Maisaka 的文本块。

    超长时优先丢弃最旧的行，保留最近的对话。

    Args:
        entries: 按时间正序排列的缓存消息。
        prefix: 前缀模板，支持 ``{count}`` 与 ``{omitted}`` 占位符。
        note: 追加在整块文本末尾的指令（用于强制回复）。
        max_total_chars: 整块文本的字符上限。
        message_max_chars: 单条消息的字符上限。

    Returns:
        str: 供 ``maisaka.context.append`` 使用的文本。
    """

    tail = ("\n" + note.strip()) if (note and note.strip()) else ""
    if not entries:
        return format_context_prefix(prefix, 0) + "\n（暂无缓存消息）" + tail

    lines: List[str] = []
    for entry in entries:
        try:
            stamp = datetime.fromtimestamp(entry.ts).strftime("%H:%M")
        except (OverflowError, OSError, ValueError):
            stamp = "--:--"
        text = entry.text.strip() or "[空消息]"
        if len(text) > message_max_chars:
            text = text[: max(1, message_max_chars - 1)] + "…"
        speaker = entry.nickname.strip() or entry.user_id or "未知"
        lines.append(f"{stamp} {speaker}：{text}")

    omitted = 0
    while True:
        header = format_context_prefix(
            prefix,
            len(lines),
            f"（已省略较早的 {omitted} 条）" if omitted else "",
        )
        total = len(header) + 1 + sum(len(line) + 1 for line in lines)
        if total <= max_total_chars or len(lines) <= 1:
            break
        lines.pop(0)
        omitted += 1
    return header + "\n" + "\n".join(lines) + tail


# ======================================================================
# 配置模型
# ======================================================================


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="总开关。关闭后插件完全不介入，所有群恢复 MaiBot 原生行为（含命令）。",
    )
    config_version: str = Field(default="1.0.0", description="配置结构版本，由插件维护，通常不需要修改。")


class ContextSectionConfig(PluginConfigBase):
    """上下文注入配置。"""

    __ui_label__ = "上下文注入"
    __ui_icon__ = "history"
    __ui_order__ = 1

    size: int = Field(
        default=20,
        ge=1,
        le=50,
        description="被 @ 时注入的最近消息条数，同时也是每个群缓存的消息上限。",
    )
    prefix: str = Field(
        default=DEFAULT_CONTEXT_PREFIX,
        max_length=200,
        description=(
            "注入内容的前缀/标题，支持 {count}（条数）与 {omitted}（省略提示）两个占位符；"
            "留空则使用默认前缀。"
        ),
    )
    include_bot_messages: bool = Field(
        default=False,
        description="是否把机器人自己发出的消息也计入缓存（多数适配器不会回显，保持关闭即可）。",
    )
    message_max_chars: int = Field(
        default=300,
        ge=40,
        le=2000,
        description="单条缓存消息在注入文本里的最大字符数，超出部分截断。",
    )
    max_total_chars: int = Field(
        default=4000,
        ge=200,
        le=20000,
        description="整块注入文本的最大字符数，超出时从最旧的消息开始省略。",
    )
    max_sessions: int = Field(
        default=200,
        ge=20,
        le=2000,
        description="最多缓存多少个会话，超出后按最近活跃时间淘汰最久未使用的群。",
    )
    cache_ttl_hours: int = Field(
        default=72,
        ge=0,
        le=8760,
        description=(
            "缓存自动淘汰时间（小时）：某个群超过这么久没有新消息，就把它的聊天缓存从内存和 "
            "context_cache.json 中整体删除（后台定时执行，重启时也会先清一次）。"
            "设为 0 表示永不过期，只能手动清空。"
        ),
    )
    flush_interval_seconds: float = Field(
        default=3.0,
        ge=0.5,
        le=60.0,
        description="上下文缓存落盘间隔（秒），有人被 @ 时不必等待落盘也能注入。",
    )


class CommandSectionConfig(PluginConfigBase):
    """群内命令配置。"""

    __ui_label__ = "群内命令"
    __ui_icon__ = "terminal"
    __ui_order__ = 2

    on_words: List[str] = Field(
        default_factory=lambda: ["/on"],
        description="把本群加入白名单的命令词，需 @ 机器人后发送，可与 /on 同时存在多个别名。",
    )
    off_words: List[str] = Field(
        default_factory=lambda: ["/off"],
        description="把本群移出白名单的命令词，需 @ 机器人后发送。",
    )
    reply_on_toggle: bool = Field(
        default=True,
        description="执行 /on、/off 后是否在群里回复一条确认消息。",
    )
    reply_added: str = Field(
        default="好哦，本群已加入白名单～之后麦麦会像平常一样参与聊天，插件不再拦截消息。",
        description="群被加入白名单后的回复。",
    )
    reply_removed: str = Field(
        default="收到，本群已移出白名单。之后麦麦只会在被 @ 时回复。",
        description="群被移出白名单后的回复。",
    )
    reply_already: str = Field(
        default="本群已经在白名单里啦，不用重复添加。",
        description="重复执行 /on 时的回复。",
    )
    reply_not_in: str = Field(
        default="本群不在白名单里，无需移除。",
        description="对不在白名单的群执行 /off 时的回复。",
    )
    reply_denied: str = Field(
        default="你没有权限修改本群的白名单。",
        description="权限不足时的回复。",
    )


class PermissionSectionConfig(PluginConfigBase):
    """命令权限配置。"""

    __ui_label__ = "命令权限"
    __ui_icon__ = "lock"
    __ui_order__ = 3

    mode: str = Field(
        default="all",
        description=(
            "all=任何群成员都能用 @麦麦 /on、/off 切换白名单（默认）；"
            "admin=仅 admins 列表里的 QQ 号可以，其他人发命令会收到无权限提示。"
        ),
    )
    admins: List[str] = Field(
        default_factory=list,
        description="mode=admin 时允许操作白名单的 QQ 号列表。列表为空时所有 /on、/off 都会被拒绝。",
    )

    if model_validator is not None:

        @model_validator(mode="before")
        @classmethod
        def _migrate_legacy_users(cls, data: Any) -> Any:
            """兼容 1.1.0 及更早版本的 ``permission.users`` 字段。"""

            if isinstance(data, dict):
                merged = dict(data)
                legacy = merged.pop("users", None)
                if legacy and not merged.get("admins"):
                    merged["admins"] = legacy
                return merged
            return data


class ReplySectionConfig(PluginConfigBase):
    """强制回复配置。"""

    __ui_label__ = "强制回复"
    __ui_icon__ = "reply"
    __ui_order__ = 4

    force: bool = Field(
        default=True,
        description="被 @ 时是否强制麦麦必须回复：在注入的聊天记录末尾追加一段「必须调用 reply 工具」的指令。",
    )
    force_note: str = Field(
        default=DEFAULT_FORCE_NOTE,
        max_length=2000,
        description="force=true 时追加在注入内容末尾的指令文本；留空则不加。",
    )
    fallback_enabled: bool = Field(
        default=True,
        description=(
            "兜底：如果指令发出后在 fallback_seconds 内该群一条消息都没发出去，"
            "插件直接调用 Replyer 生成一条回复并发送，保证 @ 一定有回应。"
        ),
    )
    fallback_seconds: float = Field(
        default=45.0,
        ge=10.0,
        le=300.0,
        description="兜底等待时间（秒）：这段时间内该群没有任何消息发出，才补发兜底回复。",
    )
    fallback_instruction: str = Field(
        default=(
            "请用一条简短、口语化的群聊回复回应对方刚刚的 @，不要写成文章、不要书面语，"
            "直接给出正文，不要加任何前后缀说明。"
        ),
        max_length=2000,
        description="兜底生成时追加给 Replyer 的写作要求。",
    )


class AtDetectionSectionConfig(PluginConfigBase):
    """@ 判定与兼容性配置。"""

    __ui_label__ = "判定与兼容"
    __ui_icon__ = "scan-eye"
    __ui_order__ = 5

    unresolved: str = Field(
        default="drop",
        description=(
            "无法确认机器人账号（account_id 缺失）且消息里有 @ 时："
            "drop=按非 @ 处理（丢弃），pass=放行。适配器正常上报 account_id 时此项不生效。"
        ),
    )


class AtGateConfig(PluginConfigBase):
    """苏醒吧麦麦！插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    context: ContextSectionConfig = Field(default_factory=ContextSectionConfig)
    command: CommandSectionConfig = Field(default_factory=CommandSectionConfig)
    permission: PermissionSectionConfig = Field(default_factory=PermissionSectionConfig)
    reply: ReplySectionConfig = Field(default_factory=ReplySectionConfig)
    at_detection: AtDetectionSectionConfig = Field(default_factory=AtDetectionSectionConfig)


# ======================================================================
# 持久化：白名单
# ======================================================================


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON：先写临时文件再 ``os.replace``，避免半截文件。

    Args:
        path: 目标文件路径。
        payload: 可 JSON 序列化的数据。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _read_json_file(path: Path) -> Optional[dict]:
    """读取 JSON 文件；文件不存在或内容损坏时返回 ``None``。

    Args:
        path: 目标文件路径。

    Returns:
        Optional[dict]: 解析后的字典。
    """

    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _quarantine_file(path: Path) -> None:
    """把损坏的状态文件改名备份，避免下次启动继续读坏文件。

    Args:
        path: 损坏的文件路径。
    """

    try:
        os.replace(path, path.with_name(path.name + ".bak"))
    except OSError:
        logger.warning("无法备份损坏的状态文件: %s", path)


def normalize_group_id(value: Any) -> str:
    """校验并归一化群号。

    Args:
        value: 用户输入或消息中的群号。

    Returns:
        str: 归一化后的群号。

    Raises:
        ValueError: 群号为空或格式非法。
    """

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        raise ValueError("群号不能为空")
    if not _GROUP_ID_RE.match(text):
        raise ValueError("群号应为 5-12 位数字，例如 123456789")
    return text


def normalize_qq(value: Any) -> str:
    """把配置里的 QQ 号规整成纯数字字符串（兼容 ``123456:昵称`` 这类写法）。

    Args:
        value: 原始配置值。

    Returns:
        str: 规整后的 QQ 号；无法识别时返回去空白后的原文。
    """

    text = str(value or "").strip()
    match = re.match(r"^(\d+)", text)
    return match.group(1) if match else text


def _trim_text(value: Any, limit: int) -> str:
    """裁剪可选文本字段。

    Args:
        value: 原始值。
        limit: 最大字符数。

    Returns:
        str: 裁剪后的文本。
    """

    return str(value or "").strip()[:limit]


def _source_label(source: str, user_id: str) -> str:
    """把白名单来源渲染成中文标签。

    Args:
        source: ``command`` 或 ``webui``。
        user_id: 操作者 QQ 号（命令来源时有值）。

    Returns:
        str: 展示用标签。
    """

    if source == "command":
        return f"群内命令（{user_id}）" if user_id else "群内命令"
    if source == "webui":
        return "WebUI 页面"
    return source or "未知"


@dataclass
class WhitelistEntry:
    """一条白名单记录。"""

    group_id: str
    group_name: str = ""
    note: str = ""
    added_at: str = ""
    source: str = ""
    user_id: str = ""

    def to_dict(self) -> Dict[str, str]:
        """序列化为可写盘的字典。

        Returns:
            Dict[str, str]: 序列化结果。
        """

        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "note": self.note,
            "added_at": self.added_at,
            "source": self.source,
            "user_id": self.user_id,
        }

    def to_public(self) -> Dict[str, str]:
        """转为 WebUI 表格可直接展示的字典。

        Returns:
            Dict[str, str]: 展示用字典。
        """

        return {
            "group_id": self.group_id,
            "group_name": self.group_name or "—",
            "note": self.note or "—",
            "added_at": self.added_at or "—",
            "added_by": _source_label(self.source, self.user_id),
            "added_by_label": _source_label(self.source, self.user_id),
        }


class WhitelistStore:
    """白名单的加载、查询与持久化。"""

    def __init__(self, path: Path) -> None:
        """初始化白名单存储。

        Args:
            path: 白名单 JSON 文件路径。
        """

        self._path = path
        self._groups: Dict[str, WhitelistEntry] = {}
        self.recovered_from_corruption = False

    @property
    def path(self) -> Path:
        """返回白名单文件路径。

        Returns:
            Path: 文件路径。
        """

        return self._path

    def load(self) -> None:
        """从磁盘加载白名单；文件损坏时备份并以空状态继续。"""

        data = _read_json_file(self._path)
        if data is None:
            if self._path.exists():
                _quarantine_file(self._path)
                self.recovered_from_corruption = True
            self._groups = {}
            return
        raw_groups = data.get("groups")
        if not isinstance(raw_groups, dict):
            _quarantine_file(self._path)
            self.recovered_from_corruption = True
            self._groups = {}
            return
        loaded: Dict[str, WhitelistEntry] = {}
        for group_id, raw in raw_groups.items():
            group_id_text = str(group_id or "").strip()
            if not _GROUP_ID_RE.match(group_id_text):
                continue
            record = raw if isinstance(raw, dict) else {}
            loaded[group_id_text] = WhitelistEntry(
                group_id=group_id_text,
                group_name=_trim_text(record.get("group_name"), 64),
                note=_trim_text(record.get("note"), 128),
                added_at=_trim_text(record.get("added_at"), 32),
                source=_trim_text(record.get("source"), 16),
                user_id=_trim_text(record.get("user_id"), 32),
            )
        self._groups = loaded

    def save(self) -> None:
        """把当前白名单原子写入磁盘。"""

        _atomic_write_json(
            self._path,
            {
                "version": STATE_VERSION,
                "groups": {group_id: entry.to_dict() for group_id, entry in self._groups.items()},
            },
        )

    def contains(self, group_id: str) -> bool:
        """判断群是否在白名单中。

        Args:
            group_id: 群号。

        Returns:
            bool: 是否命中。
        """

        return bool(group_id) and group_id in self._groups

    def add(
        self,
        group_id: str,
        *,
        group_name: str = "",
        note: str = "",
        source: str = "",
        user_id: str = "",
    ) -> bool:
        """把群加入白名单；已存在时只补充群名等展示信息。

        Args:
            group_id: 群号。
            group_name: 群名。
            note: 备注。
            source: 来源（``command`` / ``webui``）。
            user_id: 操作者 QQ 号。

        Returns:
            bool: 本次是否为新增。
        """

        existing = self._groups.get(group_id)
        if existing is not None:
            if group_name:
                existing.group_name = _trim_text(group_name, 64)
            if note:
                existing.note = _trim_text(note, 128)
            return False
        self._groups[group_id] = WhitelistEntry(
            group_id=group_id,
            group_name=_trim_text(group_name, 64),
            note=_trim_text(note, 128),
            added_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source=_trim_text(source, 16),
            user_id=_trim_text(user_id, 32),
        )
        return True

    def remove(self, group_id: str) -> bool:
        """把群移出白名单。

        Args:
            group_id: 群号。

        Returns:
            bool: 是否确实删除了记录。
        """

        return self._groups.pop(group_id, None) is not None

    def clear(self) -> int:
        """清空白名单。

        Returns:
            int: 被清除的群数量。
        """

        count = len(self._groups)
        self._groups.clear()
        return count

    def snapshot(self) -> List[Dict[str, str]]:
        """返回按加入时间倒序排列的白名单快照。

        Returns:
            List[Dict[str, str]]: WebUI 展示用列表。
        """

        ordered = sorted(self._groups.values(), key=lambda entry: entry.added_at, reverse=True)
        return [entry.to_public() for entry in ordered]

    def __len__(self) -> int:
        """返回白名单群数量。

        Returns:
            int: 群数量。
        """

        return len(self._groups)


# ======================================================================
# 持久化：最近消息环形缓存
# ======================================================================


@dataclass
class ContextEntry:
    """缓存中的一条群消息。"""

    nickname: str
    user_id: str
    text: str
    ts: float
    message_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可写盘的字典。

        Returns:
            Dict[str, Any]: 序列化结果。
        """

        return {
            "nickname": self.nickname,
            "user_id": self.user_id,
            "text": self.text,
            "ts": self.ts,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["ContextEntry"]:
        """从字典恢复一条缓存消息。

        Args:
            raw: 原始字典。

        Returns:
            Optional[ContextEntry]: 解析结果；数据非法时返回 ``None``。
        """

        if not isinstance(raw, dict):
            return None
        try:
            ts = float(raw.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        return cls(
            nickname=_trim_text(raw.get("nickname"), 64),
            user_id=_trim_text(raw.get("user_id"), 32),
            text=text[:1000],
            ts=ts,
            message_id=_trim_text(raw.get("message_id"), 64),
        )


class ContextBuffer:
    """按会话保存最近 N 条群消息的环形缓存。"""

    def __init__(self, limit: int = 20, max_sessions: int = 200) -> None:
        """初始化缓存。

        Args:
            limit: 每个会话保留的消息条数。
            max_sessions: 最多保留的会话数量。
        """

        self._limit = max(1, int(limit))
        self._max_sessions = max(1, int(max_sessions))
        self._sessions: "OrderedDict[str, Deque[ContextEntry]]" = OrderedDict()
        self._group_ids: Dict[str, str] = {}
        self._updated_at: Dict[str, str] = {}
        self.dirty = False
        self.recovered_from_corruption = False

    @property
    def limit(self) -> int:
        """返回每个会话的消息上限。

        Returns:
            int: 消息条数上限。
        """

        return self._limit

    def configure(self, *, limit: Optional[int] = None, max_sessions: Optional[int] = None) -> None:
        """按最新配置调整上限，并把已有缓存裁剪到新上限。

        Args:
            limit: 新的每会话消息上限。
            max_sessions: 新的会话数量上限。
        """

        if limit is not None:
            self._limit = max(1, int(limit))
        if max_sessions is not None:
            self._max_sessions = max(1, int(max_sessions))
        for entries in self._sessions.values():
            while len(entries) > self._limit:
                entries.popleft()
        while len(self._sessions) > self._max_sessions:
            session_id, _ = self._sessions.popitem(last=False)
            self._group_ids.pop(session_id, None)
            self._updated_at.pop(session_id, None)

    def record(self, session_id: str, group_id: str, entry: ContextEntry) -> None:
        """记录一条消息，必要时淘汰最久未活跃的会话。

        Args:
            session_id: 会话 ID。
            group_id: 群号。
            entry: 缓存消息。
        """

        if not session_id:
            return
        entries = self._sessions.get(session_id)
        if entries is None:
            entries = deque()
            self._sessions[session_id] = entries
        entries.append(entry)
        while len(entries) > self._limit:
            entries.popleft()
        self._sessions.move_to_end(session_id)
        self._group_ids[session_id] = group_id
        self._updated_at[session_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.dirty = True
        while len(self._sessions) > self._max_sessions:
            stale_id, _ = self._sessions.popitem(last=False)
            self._group_ids.pop(stale_id, None)
            self._updated_at.pop(stale_id, None)

    def prune_expired(self, ttl_seconds: float, now: Optional[float] = None) -> int:
        """按活跃度淘汰过期会话。

        某个会话最后一条消息的时间早于 ``now - ttl_seconds`` 时，整条会话（含消息、群号、
        更新时间记录）一并删除，避免聊天文本长期滞留在磁盘上。

        Args:
            ttl_seconds: 保留时长（秒）；小于等于 0 表示不淘汰。
            now: 当前时间戳（秒），默认取系统时间。

        Returns:
            int: 被淘汰的会话数量。
        """

        if ttl_seconds <= 0:
            return 0
        current = time.time() if now is None else float(now)
        cutoff = current - ttl_seconds
        expired = [
            session_id
            for session_id, entries in self._sessions.items()
            if not entries or entries[-1].ts < cutoff
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
            self._group_ids.pop(session_id, None)
            self._updated_at.pop(session_id, None)
        if expired:
            self.dirty = True
        return len(expired)

    def recent(self, session_id: str, limit: Optional[int] = None) -> List[ContextEntry]:
        """读取会话最近的消息。

        Args:
            session_id: 会话 ID。
            limit: 最多返回多少条，默认使用配置上限。

        Returns:
            List[ContextEntry]: 按时间正序排列的消息。
        """

        entries = self._sessions.get(session_id)
        if not entries:
            return []
        count = self._limit if limit is None else max(0, int(limit))
        if count <= 0:
            return []
        start = max(0, len(entries) - count)
        return list(entries)[start:]

    def clear(self) -> int:
        """清空所有缓存。

        Returns:
            int: 被清除的会话数量。
        """

        count = len(self._sessions)
        self._sessions.clear()
        self._group_ids.clear()
        self._updated_at.clear()
        self.dirty = True
        return count

    def session_count(self) -> int:
        """返回已缓存会话数量。

        Returns:
            int: 会话数量。
        """

        return len(self._sessions)

    def message_count(self) -> int:
        """返回已缓存消息总数。

        Returns:
            int: 消息总数。
        """

        return sum(len(entries) for entries in self._sessions.values())

    def to_payload(self) -> Dict[str, Any]:
        """序列化为可写盘的结构。

        Returns:
            Dict[str, Any]: 序列化结果。
        """

        return {
            "version": STATE_VERSION,
            "sessions": {
                session_id: {
                    "group_id": self._group_ids.get(session_id, ""),
                    "updated_at": self._updated_at.get(session_id, ""),
                    "messages": [entry.to_dict() for entry in entries],
                }
                for session_id, entries in self._sessions.items()
                if entries
            },
        }

    def load(self, path: Path) -> None:
        """从磁盘恢复缓存；文件损坏时备份并以空状态继续。

        Args:
            path: 缓存文件路径。
        """

        data = _read_json_file(path)
        if data is None:
            if path.exists():
                _quarantine_file(path)
                self.recovered_from_corruption = True
            return
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            _quarantine_file(path)
            self.recovered_from_corruption = True
            return
        for session_id, raw in sessions.items():
            session_text = str(session_id or "").strip()
            if not session_text or not isinstance(raw, dict):
                continue
            raw_messages = raw.get("messages")
            if not isinstance(raw_messages, list):
                continue
            entries: Deque[ContextEntry] = deque()
            for item in raw_messages[-self._limit :]:
                entry = ContextEntry.from_dict(item)
                if entry is not None:
                    entries.append(entry)
            if not entries:
                continue
            self._sessions[session_text] = entries
            self._group_ids[session_text] = _trim_text(raw.get("group_id"), 32)
            self._updated_at[session_text] = _trim_text(raw.get("updated_at"), 32)
        while len(self._sessions) > self._max_sessions:
            stale_id, _ = self._sessions.popitem(last=False)
            self._group_ids.pop(stale_id, None)
            self._updated_at.pop(stale_id, None)
        self.dirty = False

    def save(self, path: Path) -> None:
        """把缓存原子写入磁盘，并清除脏标记。

        Args:
            path: 缓存文件路径。
        """

        _atomic_write_json(path, self.to_payload())
        self.dirty = False


# ======================================================================
# 插件主体
# ======================================================================


class AtGatePlugin(MaiBotPlugin):
    """苏醒吧麦麦！与白名单插件。"""

    config_model = AtGateConfig

    def __init__(self) -> None:
        """初始化插件内部状态。"""

        super().__init__()
        self._loaded = False
        self._whitelist: Optional[WhitelistStore] = None
        self._buffer: Optional[ContextBuffer] = None
        self._data_dir: Optional[Path] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._mutation_lock = asyncio.Lock()
        # 群号 -> 首次发现身份无法确认的时间，用于 WebUI 展示与去重告警
        self._unresolved_groups: Dict[str, str] = {}
        # 兜底强制回复：会话 ID -> 本次 @ 的令牌，用来判断是否已被更新的 @ 覆盖
        self._force_tokens: Dict[str, str] = {}
        # 令牌 -> 后台兜底任务，卸载时统一取消
        self._force_tasks: Dict[str, asyncio.Task] = {}
        # 会话 ID -> 最近一次成功发出消息的时间戳，用于判断「是否已经回过」
        self._last_outbound_at: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        """加载插件：恢复白名单与上下文缓存，启动落盘任务。"""

        data_dir = Path(str(self.ctx.paths.data_dir))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir

        config = self.config
        self._whitelist = WhitelistStore(data_dir / WHITELIST_FILENAME)
        self._whitelist.load()
        if self._whitelist.recovered_from_corruption:
            self._get_logger().warning("白名单文件损坏，已备份为 .bak，本次以空白名单启动")

        self._buffer = ContextBuffer(
            limit=config.context.size,
            max_sessions=config.context.max_sessions,
        )
        self._buffer.load(data_dir / CONTEXT_CACHE_FILENAME)
        if self._buffer.recovered_from_corruption:
            self._get_logger().warning("上下文缓存文件损坏，已备份为 .bak，本次以空缓存启动")
        ttl_hours = config.context.cache_ttl_hours
        pruned = self._buffer.prune_expired(float(ttl_hours) * 3600.0)
        if pruned:
            self._get_logger().info(
                "启动时按活跃度淘汰了 %d 个超过 %d 小时无新消息的缓存会话",
                pruned,
                ttl_hours,
            )

        self._loaded = True
        self._flush_task = asyncio.create_task(self._flush_loop(), name="atgate-context-flush")
        self._get_logger().info(
            "苏醒吧麦麦！已加载：白名单 %d 个群，缓存 %d 个会话 / %d 条消息，注入上限 %d 条；"
            "命令权限=%s，强制回复=%s，超时兜底=%s（%d 秒）",
            len(self._whitelist),
            self._buffer.session_count(),
            self._buffer.message_count(),
            config.context.size,
            "admin" if str(config.permission.mode or "").strip().lower() in ("admin", "list") else "all",
            "开" if config.reply.force else "关",
            "开" if (config.reply.force and config.reply.fallback_enabled) else "关",
            int(config.reply.fallback_seconds),
        )

    async def on_unload(self) -> None:
        """卸载插件：停止落盘任务、取消兜底任务并把状态刷盘。"""

        self._loaded = False
        for task in list(self._force_tasks.values()):
            task.cancel()
        self._force_tasks.clear()
        self._force_tokens.clear()
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self._save_buffer()
        if self._whitelist is not None:
            try:
                await asyncio.to_thread(self._whitelist.save)
            except Exception as exc:  # noqa: BLE001 - 卸载阶段只记录日志
                self._get_logger().warning("白名单落盘失败: %s", exc)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：按新上限裁剪缓存。

        Args:
            scope: 变更范围。
            config_data: 最新配置数据。
            version: 配置版本。
        """

        if scope not in ("self", ""):
            return
        if self._buffer is None:
            return
        config = self.config
        self._buffer.configure(limit=config.context.size, max_sessions=config.context.max_sessions)
        self._get_logger().info(
            "配置已更新：enabled=%s，注入 %d 条，缓存 %d 个会话",
            config.plugin.enabled,
            config.context.size,
            self._buffer.session_count(),
        )

    # ------------------------------------------------------------------
    # 入站 Hook
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        name="at_gate",
        description="苏醒吧麦麦！：非 @ 消息直接丢弃，@ 时注入最近群聊上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=15000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_incoming_message(
        self,
        message: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, str]]:
        """在处理入站消息之前决定：放行、丢弃、注入或执行白名单命令。

        Args:
            message: Hook 载荷中的消息字典。
            **kwargs: 宿主可能附带的其它载荷字段。

        Returns:
            Optional[Dict[str, str]]: 返回 ``{"action": "abort"}`` 表示丢弃该消息。
        """

        if not self._loaded or self._whitelist is None or self._buffer is None:
            return None
        if not isinstance(message, dict):
            return None

        config = self.config
        group_id = group_id_of(message)
        try:
            decision = decide_message(
                message,
                whitelisted=self._whitelist.contains(group_id),
                enabled=config.plugin.enabled,
                on_words=config.command.on_words,
                off_words=config.command.off_words,
                unresolved_policy=config.at_detection.unresolved,
            )
        except Exception as exc:  # noqa: BLE001 - 判定失败时放行，避免误吞消息
            self._get_logger().warning("消息判定失败，本次放行: %s", exc)
            return None

        if not decision.identity_resolved:
            # 这是会导致「消息被静默丢弃」的配置问题，用 ERROR 级别并在 WebUI 上同步展示
            first_seen = self._unresolved_groups.get(group_id)
            if first_seen is None:
                first_seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._unresolved_groups[group_id] = first_seen
            self._get_logger().error(
                "【需要处理】群 %s 无法确认机器人账号（适配器未上报 account_id）：该群的非 @ 消息"
                "正在被按 at_detection.unresolved=%s 处理，表现为「麦麦突然不理人」。"
                "修法：让 bot_config.toml 的 qq_account 与 SnowLuma/OneBot 适配器登录的 QQ 完全一致；"
                "若适配器确实拿不到 account_id，把 at_detection.unresolved 改成 pass 即可恢复。",
                group_id,
                config.at_detection.unresolved,
            )

        if decision.action == DROP:
            self._record(message)
            return {"action": "abort"}
        if decision.action == CMD_ON:
            await self._toggle("on", message)
            return {"action": "abort"}
        if decision.action == CMD_OFF:
            await self._toggle("off", message)
            return {"action": "abort"}
        if decision.action == AT_PASS:
            block = await self._inject_context(message)
            self._record(message)
            self._arm_force_fallback(message, block)
            return None
        return None

    # ------------------------------------------------------------------
    # 出站观察：判断「麦麦是否已经回过话」，用于取消兜底
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_send",
        name="at_gate_outbound",
        description="苏醒吧麦麦！：记录出站消息，用于判断 @ 是否已经被正常回复",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_outbound_message(
        self,
        message: Optional[Dict[str, Any]] = None,
        sent: bool = False,
        **kwargs: Any,
    ) -> None:
        """观察发送结果：成功发出消息即认为该会话已经回过话。

        Args:
            message: 本次发送消息的序列化字典。
            sent: 是否发送成功。
            **kwargs: 宿主可能附带的其它字段。
        """

        if not self._loaded or not sent or not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return
        self._last_outbound_at[session_id] = time.time()
        # 已经发出过消息，本轮的兜底回复没必要再补
        self._force_tokens.pop(session_id, None)

    # ------------------------------------------------------------------
    # WebUI 页面 API
    # ------------------------------------------------------------------

    @API("webui_whitelist_snapshot", description="读取白名单与上下文缓存状态", version="1")
    async def webui_whitelist_snapshot(self) -> Dict[str, Any]:
        """为 WebUI 页面提供只读快照。

        Returns:
            Dict[str, Any]: 白名单、缓存计数与当前模式。
        """

        config = self.config
        unresolved = dict(self._unresolved_groups)
        unresolved_hint = ""
        if unresolved:
            shown = "、".join(sorted(unresolved)[:5])
            more = f" 等 {len(unresolved)} 个群" if len(unresolved) > 5 else ""
            unresolved_hint = (
                f"⚠️ 有 {len(unresolved)} 个群无法确认机器人账号（适配器未上报 account_id），"
                f"这些群的非 @ 消息正在被丢弃，表现为「麦麦突然不理人」：{shown}{more}。"
                "处理办法：让 bot_config.toml 的 qq_account 与 SnowLuma/OneBot 适配器登录的 QQ 完全一致；"
                "若适配器确实拿不到 account_id，把 at_detection.unresolved 改成 pass。"
            )
        perm_mode = str(config.permission.mode or "all").strip().lower()
        perm_mode = "admin" if perm_mode in ("admin", "list") else "all"
        admin_count = len({normalize_qq(item) for item in list(config.permission.admins or []) if normalize_qq(item)})
        if perm_mode == "admin":
            permission_hint = (
                f"当前权限模式：admin —— 只有 {admin_count} 个白名单管理员可以用 @麦麦 /on、/off"
                "（名单在插件配置的「命令权限」里填写）。"
            )
            if admin_count == 0:
                permission_hint = "当前权限模式：admin，但管理员名单是空的，所有 /on、/off 都会被拒绝，请先填写 QQ 号。"
        else:
            permission_hint = "当前权限模式：all —— 任何群成员都可以用 @麦麦 /on、/off 切换白名单。"
        return {
            "count": len(self._whitelist) if self._whitelist is not None else 0,
            "groups": self._whitelist.snapshot() if self._whitelist is not None else [],
            "cache_session_count": self._buffer.session_count() if self._buffer is not None else 0,
            "cache_message_count": self._buffer.message_count() if self._buffer is not None else 0,
            "cache_ttl_hours": config.context.cache_ttl_hours,
            "unresolved_group_count": len(unresolved),
            "unresolved_hint": unresolved_hint,
            "context_size": config.context.size,
            "mode": "drop_non_at" if config.plugin.enabled else "disabled",
            "permission_mode": perm_mode,
            "permission_mode_label": "admin" if perm_mode == "admin" else "all",
            "admin_count": admin_count,
            "permission_hint": permission_hint,
            "force_reply": bool(config.reply.force),
            "force_fallback": bool(config.reply.force and config.reply.fallback_enabled),
            "enabled": config.plugin.enabled,
        }

    @API("webui_add_group", description="把群加入白名单", version="1")
    async def webui_add_group(self, group_id: str, group_name: str = "", note: str = "") -> Dict[str, Any]:
        """从 WebUI 添加白名单。

        Args:
            group_id: 群号。
            group_name: 群名。
            note: 备注。

        Returns:
            Dict[str, Any]: 操作结果。

        Raises:
            ValueError: 群号非法。
        """

        normalized = normalize_group_id(group_id)
        if self._whitelist is None:
            raise ValueError("插件尚未加载完成，请稍后重试")
        async with self._mutation_lock:
            added = self._whitelist.add(
                normalized,
                group_name=_trim_text(group_name, 64),
                note=_trim_text(note, 128),
                source="webui",
            )
            await asyncio.to_thread(self._whitelist.save)
        self._get_logger().info("WebUI %s白名单：群 %s", "加入" if added else "更新", normalized)
        return {"success": True, "added": added, "group_id": normalized}

    @API("webui_remove_group", description="把群移出白名单", version="1")
    async def webui_remove_group(self, group_id: str) -> Dict[str, Any]:
        """从 WebUI 移出白名单。

        Args:
            group_id: 群号。

        Returns:
            Dict[str, Any]: 操作结果。

        Raises:
            ValueError: 群号非法，或该群本来就不在白名单。
        """

        normalized = normalize_group_id(group_id)
        if self._whitelist is None:
            raise ValueError("插件尚未加载完成，请稍后重试")
        async with self._mutation_lock:
            removed = self._whitelist.remove(normalized)
            if removed:
                await asyncio.to_thread(self._whitelist.save)
        if not removed:
            raise ValueError(f"群 {normalized} 不在白名单中")
        self._get_logger().info("WebUI 移出白名单：群 %s", normalized)
        return {"success": True, "removed": normalized}

    @API("webui_clear_whitelist", description="清空全部白名单", version="1")
    async def webui_clear_whitelist(self) -> Dict[str, Any]:
        """清空白名单。

        Returns:
            Dict[str, Any]: 被清除的群数量。

        Raises:
            ValueError: 插件尚未加载完成。
        """

        if self._whitelist is None:
            raise ValueError("插件尚未加载完成，请稍后重试")
        async with self._mutation_lock:
            count = self._whitelist.clear()
            if count:
                await asyncio.to_thread(self._whitelist.save)
        self._get_logger().info("WebUI 清空白名单：共 %d 个群", count)
        return {"success": True, "cleared": count}

    @API("webui_clear_cache", description="清空上下文缓存", version="1")
    async def webui_clear_cache(self) -> Dict[str, Any]:
        """清空插件缓存的最近群聊消息。

        Returns:
            Dict[str, Any]: 被清除的会话数量。

        Raises:
            ValueError: 插件尚未加载完成。
        """

        if self._buffer is None:
            raise ValueError("插件尚未加载完成，请稍后重试")
        count = self._buffer.clear()
        await self._save_buffer()
        self._get_logger().info("WebUI 清空上下文缓存：共 %d 个会话", count)
        return {"success": True, "cleared_sessions": count}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _record(self, message: Dict[str, Any]) -> None:
        """把一条群消息写入环形缓存。

        Args:
            message: Hook 载荷中的消息字典。
        """

        if self._buffer is None:
            return
        try:
            config = self.config
            session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                return
            user_id, nickname = user_of(message)
            if user_id and user_id in bot_accounts(message) and not config.context.include_bot_messages:
                return
            text = render_message_text(message) or "[非文本消息]"
            self._buffer.configure(limit=config.context.size, max_sessions=config.context.max_sessions)
            self._buffer.record(
                session_id,
                group_id_of(message),
                ContextEntry(
                    nickname=nickname or user_id,
                    user_id=user_id,
                    text=text,
                    ts=timestamp_of(message),
                    message_id=str(message.get("message_id") or ""),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 缓存失败不应影响消息处理
            self._get_logger().warning("写入上下文缓存失败: %s", exc)

    async def _inject_context(self, message: Dict[str, Any]) -> str:
        """把该群最近 N 条消息注入 Maisaka 上下文。

        Args:
            message: Hook 载荷中的消息字典。

        Returns:
            str: 实际注入的文本块（注入失败时仍返回渲染结果，供兜底生成复用）。
        """

        if self._buffer is None:
            return ""
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return ""
        config = self.config
        entries = self._buffer.recent(session_id, config.context.size)
        note = ""
        if config.reply.force and config.reply.force_note.strip():
            note = config.reply.force_note
        block = render_context(
            entries,
            prefix=config.context.prefix,
            note=note,
            max_total_chars=config.context.max_total_chars,
            message_max_chars=config.context.message_max_chars,
        )
        segments = [{"type": "text", "content": block}]
        source_kind = f"plugin:{PLUGIN_ID}:at-context"
        injected = await self._append_context(session_id, segments, block, source_kind)
        if not injected:
            # 群里第一次被 @ 时宿主还没建过会话，先建出会话再补一次注入
            if await self._ensure_session(message):
                injected = await self._append_context(session_id, segments, block, source_kind)
        if not injected:
            self._get_logger().warning(
                "群 %s 的上下文注入失败，本次 @ 仍会正常回复，但没有历史上下文",
                group_id_of(message),
            )
        return block

    async def _append_context(
        self,
        session_id: str,
        segments: List[Dict[str, Any]],
        visible_text: str,
        source_kind: str,
    ) -> bool:
        """调用宿主能力向 Maisaka 上下文追加一条消息。

        Args:
            session_id: 会话 ID。
            segments: 消息段。
            visible_text: 可见文本。
            source_kind: 来源标识。

        Returns:
            bool: 是否成功。
        """

        try:
            result = await asyncio.wait_for(
                self.ctx.maisaka.context.append(
                    session_id,
                    segments,
                    visible_text=visible_text,
                    source_kind=source_kind,
                ),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001 - 注入失败只降级，不阻断回复
            self._get_logger().debug("上下文注入调用异常: %s", exc)
            return False
        if isinstance(result, dict) and result.get("success") is False:
            self._get_logger().debug("上下文注入被宿主拒绝: %s", result.get("error"))
            return False
        return True

    async def _ensure_session(self, message: Dict[str, Any]) -> bool:
        """确保该群在宿主中存在聊天流与会话。

        Args:
            message: Hook 载荷中的消息字典。

        Returns:
            bool: 会话是否可用。
        """

        group_id = group_id_of(message)
        if not group_id:
            return False
        platform = str(message.get("platform") or "qq").strip() or "qq"
        try:
            result = await asyncio.wait_for(
                self.ctx.chat.open_session(
                    platform=platform,
                    chat_type="group",
                    user_id="",
                    group_id=group_id,
                    account_id=str(message.get("account_id") or "").strip(),
                    scope=str(message.get("scope") or "").strip(),
                ),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001 - 建流失败只影响本次注入/回执
            self._get_logger().debug("打开群 %s 会话失败: %s", group_id, exc)
            return False
        if isinstance(result, dict) and result.get("success") is False:
            self._get_logger().warning("打开群 %s 会话被拒绝: %s", group_id, result.get("error"))
            return False
        return True

    async def _reply(self, text: str, message: Dict[str, Any]) -> None:
        """在群里发送一条确认消息。

        Args:
            text: 回复文本。
            message: Hook 载荷中的消息字典。
        """

        session_id = str(message.get("session_id") or "").strip()
        if not text or not session_id:
            return
        await self._ensure_session(message)
        try:
            sent = await asyncio.wait_for(self.ctx.send.text(text, session_id), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 发不出去也不能影响状态变更
            self._get_logger().warning("群 %s 命令回执发送失败: %s", group_id_of(message), exc)
            return
        if sent is False:
            self._get_logger().warning("群 %s 命令回执发送失败（宿主返回 False）", group_id_of(message))

    # ------------------------------------------------------------------
    # 兜底强制回复：@ 之后一段时间内没有发出任何消息，就由插件补一条
    # ------------------------------------------------------------------

    def _arm_force_fallback(self, message: Dict[str, Any], block: str) -> None:
        """在 @ 放行后挂一个兜底任务：超时仍未发出消息就调用 Replyer 补一条回复。

        Args:
            message: Hook 载荷中的消息字典。
            block: 本次注入的上下文文本，兜底生成时作为写作材料。
        """

        if not self._loaded:
            return
        config = self.config.reply
        if not config.force or not config.fallback_enabled:
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return
        token = uuid.uuid4().hex
        armed_at = time.time()
        self._force_tokens[session_id] = token
        task = asyncio.create_task(
            self._force_watchdog(session_id, message, block, token, armed_at),
            name=f"atgate-force-{session_id}",
        )
        self._force_tasks[token] = task
        task.add_done_callback(lambda _task: self._force_tasks.pop(token, None))

    async def _force_watchdog(
        self,
        session_id: str,
        message: Dict[str, Any],
        block: str,
        token: str,
        armed_at: float,
    ) -> None:
        """等待兜底时间，期间该会话没有发出消息才补发。

        Args:
            session_id: 会话 ID。
            message: Hook 载荷中的消息字典。
            block: 注入的上下文文本。
            token: 本次 @ 的令牌。
            armed_at: 挂起兜底的时刻。
        """

        try:
            await self._force_wait(float(self.config.reply.fallback_seconds))
            if not self._loaded:
                return
            if self._force_tokens.get(session_id) != token:
                # 已经被更新的 @ 覆盖，或期间正常回复发出后被清理
                return
            if self._last_outbound_at.get(session_id, 0.0) >= armed_at:
                return
            self._force_tokens.pop(session_id, None)
            await self._force_generate_and_send(session_id, message, block)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - 兜底失败只记日志
            self._get_logger().warning("群 %s 的兜底回复异常: %s", group_id_of(message), exc)

    async def _force_wait(self, seconds: float) -> None:
        """兜底等待；单独抽成方法，便于测试时替换成瞬时返回。

        Args:
            seconds: 等待秒数。
        """

        await asyncio.sleep(seconds)

    async def _force_generate_and_send(
        self,
        session_id: str,
        message: Dict[str, Any],
        block: str,
    ) -> None:
        """调用 Replyer 生成一条回复并发送（兜底路径）。

        Args:
            session_id: 会话 ID。
            message: Hook 载荷中的消息字典。
            block: 注入的上下文文本。
        """

        config = self.config
        group_id = group_id_of(message)
        if not block and self._buffer is not None:
            block = render_context(
                self._buffer.recent(session_id, config.context.size),
                prefix=config.context.prefix,
                note=config.reply.force_note if config.reply.force else "",
                max_total_chars=config.context.max_total_chars,
                message_max_chars=config.context.message_max_chars,
            )
        if not block.strip():
            return
        try:
            result = await asyncio.wait_for(
                self.ctx.maisaka.reply.generate(
                    session_id,
                    block,
                    format_instruction=config.reply.fallback_instruction,
                    timeout_ms=60000,
                ),
                timeout=90.0,
            )
        except Exception as exc:  # noqa: BLE001 - 生成失败只记日志
            self._get_logger().warning("群 %s 兜底回复生成失败: %s", group_id, exc)
            return
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error") if isinstance(result, dict) else result
            self._get_logger().warning("群 %s 兜底回复生成失败: %s", group_id, error)
            return
        text = str(result.get("text") or "").strip()
        if not text:
            self._get_logger().warning("群 %s 兜底回复为空文本，已放弃", group_id)
            return
        if len(text) > 2000:
            text = text[:1999] + "…"
        await self._ensure_session(message)
        try:
            sent = await asyncio.wait_for(self.ctx.send.text(text, session_id), timeout=15.0)
        except Exception as exc:  # noqa: BLE001 - 发送失败只记日志
            self._get_logger().warning("群 %s 兜底回复发送失败: %s", group_id, exc)
            return
        if sent is False:
            self._get_logger().warning("群 %s 兜底回复发送失败（宿主返回 False）", group_id)
            return
        self._get_logger().info(
            "群 %s 在 %d 秒内没有回复 @，已由插件调用 Replyer 补发一条回复",
            group_id,
            int(config.reply.fallback_seconds),
        )

    async def _toggle(self, action: str, message: Dict[str, Any]) -> None:
        """执行白名单增删并回复确认。

        Args:
            action: ``on`` 或 ``off``。
            message: Hook 载荷中的消息字典。
        """

        group_id = group_id_of(message)
        if not group_id or self._whitelist is None:
            return
        config = self.config
        user_id, nickname = user_of(message)
        if not self._permitted(user_id):
            self._get_logger().info(
                "用户 %s（%s）在群 %s 触发 %s，但权限不足",
                user_id,
                nickname or "未知",
                group_id,
                action,
            )
            if config.command.reply_on_toggle:
                await self._reply(config.command.reply_denied, message)
            return

        group_name = group_name_of(message)
        if action == "on":
            async with self._mutation_lock:
                added = self._whitelist.add(
                    group_id,
                    group_name=group_name,
                    source="command",
                    user_id=user_id,
                )
                await self._save_whitelist_locked()
            reply = config.command.reply_added if added else config.command.reply_already
            self._get_logger().info(
                "群 %s %s白名单（命令，操作者 %s）",
                group_id,
                "加入" if added else "已在",
                user_id or "未知",
            )
        else:
            async with self._mutation_lock:
                removed = self._whitelist.remove(group_id)
                if removed:
                    await self._save_whitelist_locked()
            reply = config.command.reply_removed if removed else config.command.reply_not_in
            self._get_logger().info(
                "群 %s %s白名单（命令，操作者 %s）",
                group_id,
                "移出" if removed else "不在",
                user_id or "未知",
            )

        if config.command.reply_on_toggle:
            await self._reply(reply, message)

    def _permitted(self, user_id: str) -> bool:
        """判断操作者是否有权修改白名单。

        兼容旧版配置：``anyone`` 等同于 ``all``，``list`` 等同于 ``admin``。

        Args:
            user_id: 操作者 QQ 号。

        Returns:
            bool: 是否有权限。
        """

        config = self.config
        mode = str(config.permission.mode or "all").strip().lower()
        if mode in ("admin", "list"):
            allowed = {normalize_qq(item) for item in list(config.permission.admins or []) if normalize_qq(item)}
            if not allowed:
                self._get_logger().warning(
                    "permission.mode=admin 但 permission.admins 为空，已拒绝所有白名单命令"
                )
                return False
            return bool(user_id) and normalize_qq(user_id) in allowed
        if mode not in ("all", "anyone"):
            self._get_logger().warning("permission.mode=%s 不是已知取值，已按 all 处理", mode)
        return True

    async def _save_whitelist_locked(self) -> None:
        """在已持有变更锁的前提下把白名单落盘（调用方负责加锁）。"""

        if self._whitelist is None:
            return
        try:
            await asyncio.to_thread(self._whitelist.save)
        except Exception as exc:  # noqa: BLE001 - 落盘失败保留内存状态
            self._get_logger().warning("白名单落盘失败: %s", exc)

    async def _save_buffer(self) -> None:
        """把上下文缓存落盘。"""

        if self._buffer is None or self._data_dir is None:
            return
        try:
            await asyncio.to_thread(self._buffer.save, self._data_dir / CONTEXT_CACHE_FILENAME)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响运行
            self._get_logger().warning("上下文缓存落盘失败: %s", exc)

    async def _flush_loop(self) -> None:
        """后台定时淘汰过期会话，并把脏的上下文缓存落盘。"""

        while True:
            try:
                interval = max(0.5, float(self.config.context.flush_interval_seconds))
                await asyncio.sleep(interval)
                if self._buffer is None:
                    continue
                ttl_hours = self.config.context.cache_ttl_hours
                pruned = self._buffer.prune_expired(float(ttl_hours) * 3600.0)
                if pruned:
                    self._get_logger().info(
                        "上下文缓存按活跃度淘汰了 %d 个会话（超过 %d 小时无新消息）",
                        pruned,
                        ttl_hours,
                    )
                if not self._buffer.dirty:
                    continue
                await self._save_buffer()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不能因单次异常退出
                self._get_logger().warning("上下文缓存后台落盘异常: %s", exc)


def create_plugin() -> AtGatePlugin:
    """插件入口工厂，供 Runner 加载插件实例。

    Returns:
        AtGatePlugin: 插件实例。
    """

    return AtGatePlugin()
