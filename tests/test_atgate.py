"""群聊 @ 门卫插件的单元测试。

运行方式（需要先装好 maibot-plugin-sdk）：
    python -m unittest discover -s tests -v

测试全部使用假的 ``PluginContext``（鸭子类型），不会连接真实 MaiBot，
也不访问网络。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_plugin_module() -> Any:
    """按文件路径加载插件模块，避免与其它名为 plugin 的模块冲突。"""

    module_name = "atgate_plugin_under_test"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


atgate = _load_plugin_module()

BOT_QQ = "10000"
GROUP_ID = "123456789"
OTHER_GROUP_ID = "987654321"
SESSION_ID = "sess-abc"
USER_ID = "20001"


# ======================================================================
# 测试用假宿主
# ======================================================================


class FakeSend:
    """记录发送文本的假发送能力。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, str]] = []
        self.fail = False

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        """记录一次发送并返回成功与否。"""

        if self.fail:
            return False
        self.sent.append({"text": text, "stream_id": stream_id})
        return True


class FakeChat:
    """记录 open_session 调用的假聊天能力。"""

    def __init__(self) -> None:
        self.opened: List[Dict[str, Any]] = []
        self.fail = False

    async def open_session(self, **kwargs: Any) -> Dict[str, Any]:
        """记录建流请求并返回成功结果。"""

        self.opened.append(dict(kwargs))
        if self.fail:
            return {"success": False, "error": "boom"}
        return {
            "success": True,
            "created": True,
            "session_id": kwargs.get("group_id", ""),
            "stream": {"stream_id": kwargs.get("group_id", "")},
        }


class FakeContextAppender:
    """记录注入调用的假 Maisaka 上下文能力。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_first_only = False
        self.always_fail = False

    async def append(self, stream_id: str, segments: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """记录一次上下文注入。"""

        self.calls.append({"stream_id": stream_id, "segments": segments, **kwargs})
        if self.always_fail:
            return {"success": False, "error": "always"}
        if self.fail_first_only and len(self.calls) == 1:
            return {"success": False, "error": "no session"}
        return {"success": True}


class FakeMaisaka:
    """Maisaka 能力集合。"""

    def __init__(self) -> None:
        self.context = FakeContextAppender()


class FakePaths:
    """插件标准路径。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)
        self.runtime_dir = str(data_dir / "runtime")


class FakeContext:
    """鸭子类型的 PluginContext。"""

    def __init__(self, data_dir: Path) -> None:
        self.paths = FakePaths(data_dir)
        self.logger = logging.getLogger("atgate.tests")
        self.send = FakeSend()
        self.chat = FakeChat()
        self.maisaka = FakeMaisaka()


# ======================================================================
# 消息构造与通用基类
# ======================================================================


def make_message(
    text: str = "随便聊聊",
    *,
    is_at: bool = False,
    at: Optional[List[str]] = None,
    group_id: str = GROUP_ID,
    group_name: str = "测试群",
    user_id: str = USER_ID,
    nickname: str = "阿岚",
    session_id: str = SESSION_ID,
    account_id: str = BOT_QQ,
    message_id: str = "m1",
    is_notify: bool = False,
    extra_segments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构造一条与宿主 Hook 载荷结构一致的消息字典。"""

    raw: List[Dict[str, Any]] = []
    for target in at or []:
        raw.append({"type": "at", "data": {"target_user_id": target}})
    if extra_segments is not None:
        raw.extend(extra_segments)
    elif text:
        raw.append({"type": "text", "data": text})
    additional: Dict[str, Any] = {}
    return {
        "message_id": message_id,
        "timestamp": "1750000000",
        "platform": "qq",
        "account_id": account_id,
        "scope": "",
        "session_id": session_id,
        "is_at": is_at,
        "is_mentioned": is_at,
        "is_notify": is_notify,
        "raw_message": raw,
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": nickname, "user_cardname": ""},
            "group_info": {"group_id": group_id, "group_name": group_name},
            "additional_config": additional,
        },
    }


class PluginTestCase(unittest.TestCase):
    """为每个测试准备独立的数据目录与插件实例。"""

    def setUp(self) -> None:
        """创建临时数据目录与插件。"""

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_dir = Path(self._temp.name)
        self.plugin = atgate.AtGatePlugin()
        self.ctx = FakeContext(self.data_dir)
        self.plugin._set_context(self.ctx)
        self.plugin.set_plugin_config(self.plugin.get_default_config())

    def run_loaded(self, scenario: Any) -> Any:
        """在已加载插件的状态下运行一个协程函数，并在结束时卸载。"""

        async def runner() -> Any:
            await self.plugin.on_load()
            try:
                return await scenario(self.plugin)
            finally:
                await self.plugin.on_unload()

        return asyncio.run(runner())

    def seed_whitelist(self, group_id: str = GROUP_ID, group_name: str = "测试群") -> None:
        """直接写入白名单文件，模拟插件重启后的状态。"""

        store = atgate.WhitelistStore(self.data_dir / atgate.WHITELIST_FILENAME)
        store.load()
        store.add(group_id, group_name=group_name, source="webui")
        store.save()


# ======================================================================
# 纯函数测试
# ======================================================================


class TestMessageParsing(unittest.TestCase):
    """消息解析相关纯函数。"""

    def test_render_message_text_keeps_order_and_placeholders(self) -> None:
        """文本与占位符应按原始顺序拼成一行。"""

        message = make_message(extra_segments=[{"type": "text", "data": ""}])
        message["raw_message"] = [
            {"type": "text", "data": "看这个"},
            {"type": "image", "data": {"file": "a.png"}},
            {"type": "text", "data": "好看吗"},
            {"type": "reply", "data": {"id": "x"}},
            {"type": "emoji", "data": {"id": "e"}},
        ]
        self.assertEqual(atgate.render_message_text(message), "看这个 [图片] 好看吗 [表情]")

    def test_iter_segments_expands_seglist(self) -> None:
        """嵌套的分段列表应被展开。"""

        segments = [{"type": "seglist", "data": [{"type": "text", "data": "hi"}]}]
        self.assertEqual([segment["type"] for segment in atgate.iter_segments(segments)], ["text"])

    def test_strip_at_prefix(self) -> None:
        """@ 前缀（含多个、含空白）应被剥掉。"""

        self.assertEqual(atgate.strip_at_prefix("@麦麦 /on"), "/on")
        self.assertEqual(atgate.strip_at_prefix("  @麦麦  @麦麦   /off  "), "/off")
        self.assertEqual(atgate.strip_at_prefix("/on"), "/on")
        self.assertEqual(atgate.strip_at_prefix("@麦麦"), "")
        # 全角 ＠ 先经 NFKC 归一化再剥离
        self.assertEqual(atgate.strip_at_prefix("＠麦麦 /on"), "/on")

    def test_parse_toggle_exact_match_only(self) -> None:
        """只做整串比较，不能误命中带前缀的单词。"""

        on_words = ["/on"]
        off_words = ["/off"]
        self.assertEqual(atgate.parse_toggle("/on", on_words, off_words), "on")
        self.assertEqual(atgate.parse_toggle("/ON", on_words, off_words), "on")
        self.assertEqual(atgate.parse_toggle("／on", on_words, off_words), "on")
        self.assertEqual(atgate.parse_toggle(" /off ", on_words, off_words), "off")
        self.assertIsNone(atgate.parse_toggle("/once", on_words, off_words))
        self.assertIsNone(atgate.parse_toggle("/oncall", on_words, off_words))
        self.assertIsNone(atgate.parse_toggle("", on_words, off_words))
        self.assertIsNone(atgate.parse_toggle("on", on_words, off_words))

    def test_group_user_extraction(self) -> None:
        """群号、群名、发送者与私聊判定。"""

        message = make_message()
        self.assertEqual(atgate.group_id_of(message), GROUP_ID)
        self.assertEqual(atgate.group_name_of(message), "测试群")
        self.assertEqual(atgate.user_of(message), (USER_ID, "阿岚"))
        message["message_info"].pop("group_info")
        self.assertEqual(atgate.group_id_of(message), "")

    def test_timestamp_of_handles_ms_and_bad_values(self) -> None:
        """毫秒时间戳应换算为秒，非法值退回当前时间。"""

        message = make_message()
        message["timestamp"] = "1750000000000"
        self.assertAlmostEqual(atgate.timestamp_of(message), 1750000000.0)
        message["timestamp"] = "not-a-number"
        self.assertGreater(atgate.timestamp_of(message), 1700000000.0)

    def test_bot_accounts_reads_additional_config(self) -> None:
        """机器人账号可来自 account_id 或 additional_config。"""

        message = make_message(account_id="")
        message["message_info"]["additional_config"] = {"platform_io_account_id": BOT_QQ}
        self.assertEqual(atgate.bot_accounts(message), {BOT_QQ})

    def test_at_targets_handles_dict_and_string_data(self) -> None:
        """@ 段既可能是 dict 数据，也可能是纯字符串。"""

        message = make_message(text="")
        message["raw_message"] = [
            {"type": "at", "data": {"target_user_id": BOT_QQ}},
            {"type": "at", "data": "30002"},
            {"type": "at", "data": {"qq": 30003}},
        ]
        self.assertEqual(atgate.at_targets(message), [BOT_QQ, "30002", "30003"])

    def test_detect_at_prefers_adapter_flag(self) -> None:
        """适配器给的 is_at 应优先信任。"""

        message = make_message(is_at=True, at=[])
        self.assertEqual(atgate.detect_at(message), (True, True))

    def test_detect_at_compares_account_id(self) -> None:
        """没有 is_at 时用 @ 目标与 account_id 比对。"""

        self.assertEqual(atgate.detect_at(make_message(at=[BOT_QQ])), (True, True))
        self.assertEqual(atgate.detect_at(make_message(at=["30002"])), (False, True))

    def test_detect_at_unresolved_identity(self) -> None:
        """无法确认机器人账号时按配置策略处理。"""

        message = make_message(account_id="", at=["30002"])
        self.assertEqual(atgate.detect_at(message, "drop"), (False, False))
        self.assertEqual(atgate.detect_at(message, "pass"), (True, False))

    def test_detect_at_without_any_at(self) -> None:
        """没有 @ 也没有 account_id 时视为非 @。"""

        self.assertEqual(atgate.detect_at(make_message(account_id="")), (False, True))


class TestDecideMessage(unittest.TestCase):
    """决策函数的完整分支。"""

    def decide(self, message: Dict[str, Any], whitelisted: bool = False, enabled: bool = True, **kwargs: Any) -> Any:
        """用默认命令词调用决策函数。"""

        return atgate.decide_message(
            message,
            whitelisted=whitelisted,
            enabled=enabled,
            on_words=["/on"],
            off_words=["/off"],
            **kwargs,
        )

    def test_private_chat_passes(self) -> None:
        """私聊不干预。"""

        message = make_message()
        message["message_info"].pop("group_info")
        self.assertEqual(self.decide(message).action, atgate.PASS)

    def test_notify_passes(self) -> None:
        """通知类消息不丢弃。"""

        self.assertEqual(self.decide(make_message(is_notify=True)).action, atgate.PASS)

    def test_disabled_plugin_passes_everything(self) -> None:
        """总开关关闭时全部放行，命令也不再生效。"""

        self.assertEqual(self.decide(make_message(), enabled=False).action, atgate.PASS)
        self.assertEqual(self.decide(make_message(is_at=True, at=[BOT_QQ], text="/on"), enabled=False).action, atgate.PASS)

    def test_plain_message_in_gated_group_is_dropped(self) -> None:
        """默认群里的普通消息丢弃。"""

        self.assertEqual(self.decide(make_message()).action, atgate.DROP)

    def test_plain_message_in_whitelisted_group_passes(self) -> None:
        """白名单群里的普通消息放行。"""

        self.assertEqual(self.decide(make_message(), whitelisted=True).action, atgate.PASS)

    def test_at_message_is_forwarded(self) -> None:
        """被 @ 时放行并标记为需要注入上下文。"""

        self.assertEqual(self.decide(make_message(is_at=True, at=[BOT_QQ])).action, atgate.AT_PASS)

    def test_at_message_in_whitelisted_group_passes_without_injection(self) -> None:
        """白名单群被 @ 时插件完全退出，不注入。"""

        self.assertEqual(self.decide(make_message(is_at=True, at=[BOT_QQ]), whitelisted=True).action, atgate.PASS)

    def test_command_wins_over_whitelist(self) -> None:
        """白名单群里的 /off 必须仍然可执行。"""

        message = make_message(is_at=True, at=[BOT_QQ], text="/off")
        self.assertEqual(self.decide(message, whitelisted=True).action, atgate.CMD_OFF)

    def test_command_with_rendered_at_prefix(self) -> None:
        """宿主把 @ 渲染进文本时命令仍能识别。"""

        message = make_message(is_at=True, at=[], text="@麦麦 /on")
        self.assertEqual(self.decide(message).action, atgate.CMD_ON)

    def test_non_at_command_is_not_a_command(self) -> None:
        """没有 @ 的 /on 只是普通消息。"""

        self.assertEqual(self.decide(make_message(text="/on")).action, atgate.DROP)


class TestRenderContext(unittest.TestCase):
    """注入文本渲染。"""

    def test_empty_buffer_hint(self) -> None:
        """没有缓存时给出明确提示。"""

        self.assertIn("暂无缓存消息", atgate.render_context([]))

    def test_renders_lines_in_order(self) -> None:
        """消息按时间正序渲染，包含昵称与时间。"""

        entries = [
            atgate.ContextEntry(nickname="阿岚", user_id="1", text="第一句", ts=1750000000.0),
            atgate.ContextEntry(nickname="小明", user_id="2", text="第二句", ts=1750000060.0),
        ]
        block = atgate.render_context(entries)
        self.assertIn("群聊最近 2 条消息", block)
        self.assertLess(block.index("第一句"), block.index("第二句"))
        self.assertIn("阿岚：第一句", block)

    def test_truncates_long_message(self) -> None:
        """超长单条消息被截断。"""

        entries = [atgate.ContextEntry(nickname="阿岚", user_id="1", text="啊" * 500, ts=1750000000.0)]
        block = atgate.render_context(entries, message_max_chars=50)
        self.assertIn("…", block)
        self.assertLess(len(block), 200)

    def test_omits_oldest_lines_when_over_budget(self) -> None:
        """超出总长度预算时从最旧的消息开始省略。"""

        entries = [
            atgate.ContextEntry(nickname="阿岚", user_id="1", text=f"第{i}条" + "x" * 80, ts=1750000000.0 + i)
            for i in range(10)
        ]
        block = atgate.render_context(entries, max_total_chars=400, message_max_chars=100)
        self.assertLessEqual(len(block), 400)
        self.assertIn("已省略较早的", block)
        self.assertIn("第9条", block)


# ======================================================================
# 持久化测试
# ======================================================================


class TestWhitelistStore(unittest.TestCase):
    """白名单存储。"""

    def setUp(self) -> None:
        """创建临时目录。"""

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "whitelist.json"
        self.store = atgate.WhitelistStore(self.path)

    def test_add_is_idempotent(self) -> None:
        """重复添加同一个群返回 False 且不重复计数。"""

        self.assertTrue(self.store.add(GROUP_ID, group_name="测试群", source="command", user_id=USER_ID))
        self.assertFalse(self.store.add(GROUP_ID, source="webui"))
        self.assertEqual(len(self.store), 1)
        self.assertTrue(self.store.contains(GROUP_ID))

    def test_remove_missing_returns_false(self) -> None:
        """移除不存在的群返回 False。"""

        self.assertFalse(self.store.remove(GROUP_ID))

    def test_persistence_roundtrip(self) -> None:
        """写盘后新实例能读回全部字段。"""

        self.store.add(GROUP_ID, group_name="测试群", note="备注", source="command", user_id=USER_ID)
        self.store.save()
        reloaded = atgate.WhitelistStore(self.path)
        reloaded.load()
        snapshot = reloaded.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["group_id"], GROUP_ID)
        self.assertEqual(snapshot[0]["group_name"], "测试群")
        self.assertIn(USER_ID, snapshot[0]["added_by_label"])

    def test_corrupted_file_is_quarantined(self) -> None:
        """损坏文件被备份，插件以空状态启动。"""

        self.path.write_text("{ this is not json", encoding="utf-8")
        store = atgate.WhitelistStore(self.path)
        store.load()
        self.assertTrue(store.recovered_from_corruption)
        self.assertEqual(len(store), 0)
        self.assertTrue((self.path.parent / (self.path.name + ".bak")).exists())

    def test_invalid_group_ids_are_dropped_on_load(self) -> None:
        """加载时忽略非法群号。"""

        self.path.write_text(
            json.dumps({"version": 1, "groups": {"abc": {"group_name": "坏"}, GROUP_ID: {"group_name": "好"}}}),
            encoding="utf-8",
        )
        store = atgate.WhitelistStore(self.path)
        store.load()
        self.assertEqual(len(store), 1)
        self.assertTrue(store.contains(GROUP_ID))

    def test_normalize_group_id(self) -> None:
        """群号校验接受合法值并拒绝非法值。"""

        self.assertEqual(atgate.normalize_group_id(" 123456789 "), "123456789")
        for bad in ("", "1234", "0123456", "12345abc", "1234567890123"):
            with self.assertRaises(ValueError):
                atgate.normalize_group_id(bad)


class TestContextBuffer(unittest.TestCase):
    """环形缓存。"""

    def make_entry(self, text: str, ts: float = 0.0) -> Any:
        """构造一条缓存消息。"""

        return atgate.ContextEntry(nickname="阿岚", user_id=USER_ID, text=text, ts=ts)

    def test_trims_to_limit(self) -> None:
        """超出上限时丢弃最旧的消息。"""

        buffer = atgate.ContextBuffer(limit=3, max_sessions=10)
        for index in range(5):
            buffer.record(SESSION_ID, GROUP_ID, self.make_entry(f"m{index}", ts=float(index)))
        recent = buffer.recent(SESSION_ID)
        self.assertEqual([entry.text for entry in recent], ["m2", "m3", "m4"])

    def test_evicts_least_recently_used_session(self) -> None:
        """会话数超限时淘汰最久未活跃的会话。"""

        buffer = atgate.ContextBuffer(limit=5, max_sessions=2)
        buffer.record("s1", GROUP_ID, self.make_entry("a"))
        buffer.record("s2", GROUP_ID, self.make_entry("b"))
        buffer.record("s1", GROUP_ID, self.make_entry("c"))
        buffer.record("s3", GROUP_ID, self.make_entry("d"))
        self.assertEqual(buffer.session_count(), 2)
        self.assertEqual(buffer.recent("s2"), [])

    def test_configure_shrinks_existing(self) -> None:
        """调小上限会立即裁剪已有缓存。"""

        buffer = atgate.ContextBuffer(limit=10, max_sessions=10)
        for index in range(6):
            buffer.record(SESSION_ID, GROUP_ID, self.make_entry(f"m{index}"))
        buffer.configure(limit=2)
        self.assertEqual(len(buffer.recent(SESSION_ID)), 2)

    def test_persistence_roundtrip(self) -> None:
        """写盘后新实例能恢复会话与消息。"""

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.json"
            buffer = atgate.ContextBuffer(limit=5, max_sessions=10)
            buffer.record(SESSION_ID, GROUP_ID, self.make_entry("你好", ts=1750000000.0))
            buffer.save(path)
            reloaded = atgate.ContextBuffer(limit=5, max_sessions=10)
            reloaded.load(path)
            recent = reloaded.recent(SESSION_ID)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].text, "你好")

    def test_clear(self) -> None:
        """清空后计数归零。"""

        buffer = atgate.ContextBuffer()
        buffer.record(SESSION_ID, GROUP_ID, self.make_entry("x"))
        self.assertEqual(buffer.clear(), 1)
        self.assertEqual(buffer.message_count(), 0)

    def test_corrupted_cache_is_quarantined(self) -> None:
        """损坏的缓存文件被备份且不抛异常。"""

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.json"
            path.write_text("garbage", encoding="utf-8")
            buffer = atgate.ContextBuffer()
            buffer.load(path)
            self.assertTrue(buffer.recovered_from_corruption)


# ======================================================================
# 插件集成测试（假宿主）
# ======================================================================


class TestHookBehaviour(PluginTestCase):
    """入站 Hook 的端到端行为。"""

    def test_plain_message_is_aborted_and_buffered(self) -> None:
        """非 @ 消息被丢弃并写入缓存，且不发送任何消息。"""

        async def scenario(plugin: Any) -> None:
            result = await plugin.handle_incoming_message(message=make_message("今天吃什么呢"))
            self.assertEqual(result, {"action": "abort"})
            self.assertEqual(plugin._buffer.message_count(), 1)
            self.assertEqual(self.ctx.send.sent, [])
            self.assertEqual(self.ctx.maisaka.context.calls, [])

        self.run_loaded(scenario)

    def test_at_message_injects_recent_context(self) -> None:
        """被 @ 时注入最近消息并放行。"""

        async def scenario(plugin: Any) -> None:
            for index in range(3):
                await plugin.handle_incoming_message(
                    message=make_message(f"闲聊{index}", message_id=f"m{index}")
                )
            result = await plugin.handle_incoming_message(
                message=make_message("在吗", is_at=True, at=[BOT_QQ], message_id="m-at")
            )
            self.assertIsNone(result)
            self.assertEqual(len(self.ctx.maisaka.context.calls), 1)
            call = self.ctx.maisaka.context.calls[0]
            self.assertEqual(call["stream_id"], SESSION_ID)
            content = call["segments"][0]["content"]
            self.assertIn("闲聊0", content)
            self.assertIn("闲聊2", content)
            self.assertNotIn("在吗", content)
            # @ 消息本身也会进入缓存，供下一次注入
            self.assertEqual(plugin._buffer.message_count(), 4)

        self.run_loaded(scenario)

    def test_at_message_retries_after_opening_session(self) -> None:
        """首次 @ 时宿主还没有会话，注入失败后建流并重试。"""

        async def scenario(plugin: Any) -> None:
            self.ctx.maisaka.context.fail_first_only = True
            result = await plugin.handle_incoming_message(
                message=make_message("在吗", is_at=True, at=[BOT_QQ])
            )
            self.assertIsNone(result)
            self.assertEqual(len(self.ctx.maisaka.context.calls), 2)
            self.assertEqual(len(self.ctx.chat.opened), 1)
            self.assertEqual(self.ctx.chat.opened[0]["group_id"], GROUP_ID)
            self.assertEqual(self.ctx.chat.opened[0]["chat_type"], "group")
            self.assertEqual(self.ctx.chat.opened[0]["account_id"], BOT_QQ)

        self.run_loaded(scenario)

    def test_injection_failure_does_not_block_reply(self) -> None:
        """注入始终失败时也放行 @ 消息，不抛异常。"""

        async def scenario(plugin: Any) -> None:
            self.ctx.maisaka.context.always_fail = True
            result = await plugin.handle_incoming_message(
                message=make_message("在吗", is_at=True, at=[BOT_QQ])
            )
            self.assertIsNone(result)
            self.assertGreaterEqual(len(self.ctx.maisaka.context.calls), 1)

        self.run_loaded(scenario)

    def test_command_on_adds_group_and_replies(self) -> None:
        """@麦麦 /on 加入白名单并回复。"""

        async def scenario(plugin: Any) -> None:
            result = await plugin.handle_incoming_message(
                message=make_message("/on", is_at=True, at=[BOT_QQ])
            )
            self.assertEqual(result, {"action": "abort"})
            self.assertTrue(plugin._whitelist.contains(GROUP_ID))
            self.assertEqual(plugin._whitelist.snapshot()[0]["group_name"], "测试群")
            self.assertEqual(len(self.ctx.send.sent), 1)
            self.assertIn("白名单", self.ctx.send.sent[0]["text"])
            self.assertEqual(self.ctx.send.sent[0]["stream_id"], SESSION_ID)
            self.assertTrue((self.data_dir / atgate.WHITELIST_FILENAME).exists())
            self.assertEqual(plugin._buffer.message_count(), 0)

        self.run_loaded(scenario)

    def test_command_on_is_idempotent(self) -> None:
        """重复 /on 只提示已在白名单。"""

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            self.assertEqual(len(self.ctx.send.sent), 2)
            self.assertIn("已经在白名单", self.ctx.send.sent[1]["text"])
            self.assertEqual(len(plugin._whitelist), 1)

        self.run_loaded(scenario)

    def test_command_off_works_inside_whitelisted_group(self) -> None:
        """白名单群里的 @麦麦 /off 能把群移出。"""

        self.seed_whitelist()

        async def scenario(plugin: Any) -> None:
            self.assertTrue(plugin._whitelist.contains(GROUP_ID))
            result = await plugin.handle_incoming_message(
                message=make_message("/off", is_at=True, at=[BOT_QQ])
            )
            self.assertEqual(result, {"action": "abort"})
            self.assertFalse(plugin._whitelist.contains(GROUP_ID))
            self.assertEqual(len(self.ctx.send.sent), 1)

        self.run_loaded(scenario)

    def test_command_off_on_absent_group_is_harmless(self) -> None:
        """对不在白名单的群执行 /off 只回复提示。"""

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/off", is_at=True, at=[BOT_QQ]))
            self.assertFalse(plugin._whitelist.contains(GROUP_ID))
            self.assertIn("无需移除", self.ctx.send.sent[0]["text"])

        self.run_loaded(scenario)

    def test_command_without_at_is_ignored(self) -> None:
        """没有 @ 的 /on 只是普通消息，不改变白名单。"""

        async def scenario(plugin: Any) -> None:
            result = await plugin.handle_incoming_message(message=make_message("/on"))
            self.assertEqual(result, {"action": "abort"})
            self.assertEqual(len(plugin._whitelist), 0)
            self.assertEqual(self.ctx.send.sent, [])

        self.run_loaded(scenario)

    def test_whitelisted_group_is_completely_untouched(self) -> None:
        """白名单群里插件不丢消息、不注入、不记录缓存。"""

        self.seed_whitelist()

        async def scenario(plugin: Any) -> None:
            result = await plugin.handle_incoming_message(message=make_message("大家好啊"))
            self.assertIsNone(result)
            self.assertEqual(plugin._buffer.message_count(), 0)
            self.assertEqual(self.ctx.maisaka.context.calls, [])
            result = await plugin.handle_incoming_message(
                message=make_message("麦麦", is_at=True, at=[BOT_QQ])
            )
            self.assertIsNone(result)
            self.assertEqual(self.ctx.maisaka.context.calls, [])

        self.run_loaded(scenario)

    def test_notify_and_private_messages_pass(self) -> None:
        """通知与私聊消息直接放行。"""

        async def scenario(plugin: Any) -> None:
            self.assertIsNone(await plugin.handle_incoming_message(message=make_message(is_notify=True)))
            private = make_message()
            private["message_info"].pop("group_info")
            self.assertIsNone(await plugin.handle_incoming_message(message=private))

        self.run_loaded(scenario)

    def test_permission_list_rejects_unlisted_user(self) -> None:
        """permission.mode=list 时非名单用户被拒绝。"""

        config = self.plugin.get_default_config()
        config["permission"]["mode"] = "list"
        config["permission"]["users"] = ["99999"]
        self.plugin.set_plugin_config(config)

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            self.assertEqual(len(plugin._whitelist), 0)
            self.assertIn("权限", self.ctx.send.sent[0]["text"])

        self.run_loaded(scenario)

    def test_permission_list_allows_listed_user(self) -> None:
        """名单内用户可以执行命令。"""

        config = self.plugin.get_default_config()
        config["permission"]["mode"] = "list"
        config["permission"]["users"] = [USER_ID]
        self.plugin.set_plugin_config(config)

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            self.assertTrue(plugin._whitelist.contains(GROUP_ID))

        self.run_loaded(scenario)

    def test_empty_user_list_rejects_everyone(self) -> None:
        """mode=list 但名单为空时安全拒绝。"""

        config = self.plugin.get_default_config()
        config["permission"]["mode"] = "list"
        config["permission"]["users"] = []
        self.plugin.set_plugin_config(config)

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            self.assertEqual(len(plugin._whitelist), 0)

        self.run_loaded(scenario)

    def test_disabled_plugin_passes_everything(self) -> None:
        """总开关关闭时 Hook 一律放行，命令不再生效。"""

        config = self.plugin.get_default_config()
        config["plugin"]["enabled"] = False
        self.plugin.set_plugin_config(config)

        async def scenario(plugin: Any) -> None:
            self.assertIsNone(await plugin.handle_incoming_message(message=make_message("普通消息")))
            self.assertIsNone(
                await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))
            )
            self.assertEqual(len(plugin._whitelist), 0)

        self.run_loaded(scenario)

    def test_unresolved_identity_policy(self) -> None:
        """无法确认机器人账号时按配置决定丢弃或放行。"""

        async def scenario(plugin: Any) -> None:
            message = make_message("看看这个", account_id="", at=[USER_ID])
            self.assertEqual(await plugin.handle_incoming_message(message=message), {"action": "abort"})

        self.run_loaded(scenario)

    def test_context_size_is_respected(self) -> None:
        """注入条数受 context.size 限制。"""

        config = self.plugin.get_default_config()
        config["context"]["size"] = 3
        self.plugin.set_plugin_config(config)

        async def scenario(plugin: Any) -> None:
            for index in range(6):
                await plugin.handle_incoming_message(message=make_message(f"消息{index}"))
            await plugin.handle_incoming_message(
                message=make_message("在吗", is_at=True, at=[BOT_QQ])
            )
            content = self.ctx.maisaka.context.calls[0]["segments"][0]["content"]
            self.assertIn("消息3", content)
            self.assertIn("消息5", content)
            self.assertNotIn("消息0", content)
            self.assertIn("最近 3 条", content)

        self.run_loaded(scenario)


class TestPersistenceAcrossReload(PluginTestCase):
    """插件重载后的状态恢复。"""

    def test_whitelist_survives_reload(self) -> None:
        """命令写入的白名单在重新加载后仍然存在。"""

        async def first(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("/on", is_at=True, at=[BOT_QQ]))

        self.run_loaded(first)

        self.plugin = atgate.AtGatePlugin()
        self.ctx = FakeContext(self.data_dir)
        self.plugin._set_context(self.ctx)
        self.plugin.set_plugin_config(self.plugin.get_default_config())

        async def second(plugin: Any) -> None:
            self.assertTrue(plugin._whitelist.contains(GROUP_ID))
            result = await plugin.handle_incoming_message(message=make_message("普通消息"))
            self.assertIsNone(result)

        self.run_loaded(second)

    def test_context_cache_survives_reload(self) -> None:
        """缓存落盘后重新加载仍可用于注入。"""

        async def first(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("缓存里的话"))

        self.run_loaded(first)

        self.plugin = atgate.AtGatePlugin()
        self.ctx = FakeContext(self.data_dir)
        self.plugin._set_context(self.ctx)
        self.plugin.set_plugin_config(self.plugin.get_default_config())

        async def second(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("在吗", is_at=True, at=[BOT_QQ]))
            content = self.ctx.maisaka.context.calls[0]["segments"][0]["content"]
            self.assertIn("缓存里的话", content)

        self.run_loaded(second)


class TestWebUiApi(PluginTestCase):
    """WebUI 页面 API。"""

    def test_add_snapshot_remove_clear(self) -> None:
        """页面接口的完整增删查流程。"""

        async def scenario(plugin: Any) -> None:
            empty = await plugin.webui_whitelist_snapshot()
            self.assertEqual(empty["count"], 0)
            self.assertEqual(empty["groups"], [])

            added = await plugin.webui_add_group(group_id=GROUP_ID, group_name="测试群", note="备注")
            self.assertTrue(added["added"])

            snapshot = await plugin.webui_whitelist_snapshot()
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["groups"][0]["group_name"], "测试群")
            self.assertEqual(snapshot["groups"][0]["added_by_label"], "WebUI 页面")
            self.assertEqual(snapshot["groups"][0]["note"], "备注")

            duplicate = await plugin.webui_add_group(group_id=GROUP_ID)
            self.assertFalse(duplicate["added"])
            self.assertEqual((await plugin.webui_whitelist_snapshot())["count"], 1)

            removed = await plugin.webui_remove_group(group_id=GROUP_ID)
            self.assertEqual(removed["removed"], GROUP_ID)
            self.assertEqual((await plugin.webui_whitelist_snapshot())["count"], 0)

        self.run_loaded(scenario)

    def test_webui_add_affects_hook_immediately(self) -> None:
        """WebUI 添加后，入站 Hook 立即按白名单放行。"""

        async def scenario(plugin: Any) -> None:
            await plugin.webui_add_group(group_id=GROUP_ID)
            self.assertIsNone(await plugin.handle_incoming_message(message=make_message("普通消息")))

        self.run_loaded(scenario)

    def test_remove_unknown_group_raises(self) -> None:
        """移出不在白名单的群给出明确错误。"""

        async def scenario(plugin: Any) -> None:
            with self.assertRaises(ValueError):
                await plugin.webui_remove_group(group_id=OTHER_GROUP_ID)

        self.run_loaded(scenario)

    def test_invalid_group_id_raises(self) -> None:
        """非法群号被拒绝。"""

        async def scenario(plugin: Any) -> None:
            with self.assertRaises(ValueError):
                await plugin.webui_add_group(group_id="abc")

        self.run_loaded(scenario)

    def test_clear_whitelist_and_cache(self) -> None:
        """清空白名单与缓存。"""

        async def scenario(plugin: Any) -> None:
            await plugin.handle_incoming_message(message=make_message("缓存一条"))
            await plugin.webui_add_group(group_id=GROUP_ID)
            cleared = await plugin.webui_clear_whitelist()
            self.assertEqual(cleared["cleared"], 1)
            cache = await plugin.webui_clear_cache()
            self.assertEqual(cache["cleared_sessions"], 1)
            snapshot = await plugin.webui_whitelist_snapshot()
            self.assertEqual(snapshot["count"], 0)
            self.assertEqual(snapshot["cache_session_count"], 0)

        self.run_loaded(scenario)

    def test_snapshot_reports_mode(self) -> None:
        """快照包含当前模式与注入上限。"""

        async def scenario(plugin: Any) -> None:
            snapshot = await plugin.webui_whitelist_snapshot()
            self.assertEqual(snapshot["mode"], "drop_non_at")
            self.assertEqual(snapshot["context_size"], 20)
            self.assertEqual(snapshot["permission_mode"], "anyone")

        self.run_loaded(scenario)


if __name__ == "__main__":
    unittest.main()
