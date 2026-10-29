# 苏醒吧麦麦！

> 控制麦麦为默认回复 only@，添加到白名单群聊正常回复。

MaiBot 第三方插件（`github.natsuyutori.wake-up-maimai`）。默认**只响应群里的 @ 消息**，被 @ 时把该群最近 20 条聊天记录注入 Maisaka 上下文；通过群内命令或 WebUI 页面维护白名单，白名单群内插件完全退出、麦麦按原生逻辑参与聊天。

## 行为表

| 群状态 | 普通消息（不 @） | @ 麦麦 |
|---|---|---|
| 默认（不在白名单） | **直接丢弃**：不入库、不进 Maisaka、不回复 | 注入该群最近 N 条消息后放行，由 Maisaka 正常回复 |
| 在白名单 | 插件完全不介入，Maisaka 原生处理（可主动接话） | 插件完全不介入（**不注入**），Maisaka 原生处理 |

群内 `@麦麦 /on` 把本群加入白名单，`@麦麦 /off` 移出；WebUI 插件页可做同样的事，两边共享同一份持久化数据。

## 环境要求

- MaiBot `1.3.x`（manifest 声明 `1.3.0`–`1.3.99`；若你用的是 1.4+，把 `_manifest.json` 里的 `host_application.max_version` 调到 `1.4.99` 即可）
- 插件 SDK `2.8+`（MaiBot 自带）
- 统一 QQ 连接器 `MaiBot-SnowLuma-Adapter`（SnowLuma / NapCat），`platform = "qq"`
- **`bot_config.toml` 的 `qq_account` 必须与适配器登录的 QQ 一致**，否则插件无法判定「这条消息 @ 的是我」

## 安装

1. **插件商城安装**（推荐）：MaiBot WebUI → 插件管理 → 插件商城，搜索「苏醒吧麦麦！」安装并启用。
2. **手动安装**：把整个 `wake-up-maimai/` 目录放到 MaiBot 的 `plugins/` 下（与其它插件同级）。
3. 在 WebUI 插件管理里启用 / 重载该插件（或重启 MaiBot）。
4. 首次加载会在插件目录生成 `config.toml`（由 `config_model` 自动生成，无需手写），确认 `[plugin] enabled = true`。
5. 打开 WebUI，左侧「苏醒吧麦麦！」工作区页面即为白名单管理页。

加载成功后日志应有：

```
苏醒吧麦麦！已加载：白名单 0 个群，缓存 0 个会话 / 0 条消息，注入上限 20 条
```

## 群内命令

| 命令 | 作用 |
|---|---|
| `@麦麦 /on` | 本群加入白名单，之后插件不再拦截本群消息 |
| `@麦麦 /off` | 本群移出白名单，之后恢复「只响应 @」 |

- 必须 **@ 机器人** 才生效；不带 @ 的 `/on` 只是普通聊天内容。
- 命令词可在配置里改（`command.on_words` / `off_words`），支持多别名。
- 命令消息本身会被拦截，不会进入 Maisaka，所以麦麦不会「回答」你的命令；插件会回一条确认（可关）。
- 权限：默认任何群成员都能切换；把 `permission.mode` 改成 `list` 并填 `permission.users`（QQ 号列表）即可收紧。`list` 但名单为空时会拒绝所有人并打 WARNING（防止误配成全员可操作）。

## WebUI 页面

页面提供：

- **统计**：白名单群数、已缓存会话数、已缓存消息数
- **添加**：填群号（必填）、群名（可选）、备注（可选）→ 加入白名单
- **表格**：点击某行打开详情，可在详情里「移出白名单」（二次确认）
- **清空白名单**、**清空上下文缓存**（均有二次确认）

页面上的增删与群内 `/on`、`/off` 操作同一份数据，改完立即对入站消息生效，无需重载插件。

## 配置项

配置文件：`plugins/wake-up-maimai/config.toml`（首次加载生成，也可在 WebUI 配置页编辑）。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 总开关。关闭后插件完全不介入（连命令也不生效），所有群恢复原生行为 |
| `context.size` | `20` | 被 @ 时注入的最近消息条数，同时是每群缓存上限（1–50） |
| `context.include_bot_messages` | `false` | 机器人自己的消息是否计入缓存（多数适配器不回显，保持关闭） |
| `context.message_max_chars` | `300` | 注入文本里单条消息的截断长度 |
| `context.max_total_chars` | `4000` | 整块注入文本上限，超出时从最旧的消息开始省略 |
| `context.max_sessions` | `200` | 最多缓存多少个群，超出按最久未活跃淘汰 |
| `context.flush_interval_seconds` | `3.0` | 缓存落盘间隔（内存中的缓存即时可用，不必等落盘） |
| `command.on_words` | `["/on"]` | 加入白名单的命令词 |
| `command.off_words` | `["/off"]` | 移出白名单的命令词 |
| `command.reply_on_toggle` | `true` | 执行命令后是否群里回执 |
| `command.reply_added` / `reply_removed` / `reply_already` / `reply_not_in` / `reply_denied` | 内置文案 | 五种回执文本 |
| `permission.mode` | `anyone` | `anyone`=任何群成员；`list`=仅 `users` 内 QQ |
| `permission.users` | `[]` | `mode=list` 时生效 |
| `at_detection.unresolved` | `drop` | 拿不到机器人账号时：`drop`=按非 @ 处理，`pass`=放行 |

## 数据与隐私

持久数据目录：`<MaiBot 根>/data/plugins/github.natsuyutori.wake-up-maimai/`

| 文件 | 内容 |
|---|---|
| `whitelist.json` | 白名单：群号、群名、备注、加入时间、来源（命令 / WebUI）、操作者 |
| `context_cache.json` | 每个群最近 N 条消息的**文本**，非文本消息记为 `[图片]`、`[表情]` 等占位符 |

- 只保存文本与占位符，不保存图片 / 语音 / 文件的二进制内容。
- 两个文件都是原子写（临时文件 + `os.replace`），文件损坏时会备份为 `.bak` 并以空状态启动（日志 WARNING）。
- 卸载插件不会删除数据；需要清理时用页面「清空白名单」「清空上下文缓存」，或手动删除上述两个文件。
- 被丢弃的普通消息**不会**进入 MaiBot 的聊天记录、记忆、学习与统计——这是「丢弃」的必然结果，加入白名单后恢复正常。

## 实现要点（想改代码时看）

- **丢弃时机**：`chat.receive.before_process` 返回 `{"action": "abort"}`。宿主在 `SessionMessage.process()` 之前处理该结果并直接 `return`，因此消息既不落库也不进 Maisaka。正因如此，20 条上下文只能由插件自建缓存提供，无法从宿主数据库回读。
- **为什么不用 `@Command`**：宿主用 `processed_plain_text` 做 `search` 匹配，而 @ 段会被渲染成 `@名片` 前缀，`^/on$` 匹配不到 `@麦麦 /on`。所以命令在 Hook 内解析 `raw_message` 的文本段（先剥 @ 前缀，再整串比对，避免 `/once` 误命中）。
- **首次 @ 的会话**：群里第一条消息就是 @ 时，宿主还没有该群会话；插件先 `chat.open_session` 建流（群会话 ID 只由 platform/account_id/scope/group_id 决定，与发送者无关），再 `maisaka.context.append` 注入并重试。
- **注入失败不阻断**：注入超时 / 被策略拒绝时只打 WARNING，@ 消息照常放行，退化为「无上下文的正常回复」。
- **单文件实现**：插件加载器按文件路径导入 `plugin.py`，不会把插件目录加入 `sys.path`，所以逻辑全部放在单文件内，测试通过 `importlib` 从路径加载。

## 验收清单

装好后按顺序验证（括号内是预期结果）：

1. 启用插件，日志出现「苏醒吧麦麦！已加载」。
2. 在**非白名单**群发一条普通消息（机器人不回复；WebUI 聊天记录里也查不到这条）。
3. 连发 3 条不同内容的闲聊，然后 `@麦麦 在吗`（回复能自然提到前 3 条里的内容）。
4. `@麦麦 /on`（收到「已加入白名单」回执）。
5. 再发普通消息（麦麦恢复参与，可能主动接话）。
6. 打开 WebUI 页面（表格里出现该群，来源显示「群内命令（QQ 号）」）。
7. `@麦麦 /off`（收到「已移出白名单」，之后普通消息不再被回复）。
8. WebUI 里手动添加一个群 → 该群立即放行；再移出 → 立即恢复门禁。
9. 重启 MaiBot（白名单与缓存都还在，页面数据不变）。
10. 私聊机器人、群内戳一戳等通知消息不受影响；发图片 / 表情 / 引用消息不报错。

## 排障

| 现象 | 排查 |
|---|---|
| 机器人完全不回复任何消息 | 插件 `enabled` 是否为 true；该群是否在白名单（白名单群不走本插件，但 MaiBot 本身也可能因频率/策略不回复）；看日志有没有「无法确认机器人账号」 |
| @ 了也不回复 | 日志 WARNING「群 xxx 中无法确认机器人账号」说明 `account_id` 缺失或不一致：核对 `bot_config.toml` 的 `qq_account` 与 SnowLuma 里登录的 QQ；也可临时把 `at_detection.unresolved` 设为 `pass` 验证 |
| 命令没反应 | 是否 @ 了机器人；`command.on_words` 是否被改；`permission.mode=list` 时自己是否在 `permission.users` 里（日志会写「权限不足」） |
| 被 @ 有回复但没有历史上下文 | 日志有「上下文注入失败」：首次建流失败或适配器策略拒绝；群里先随便聊几条再 @ 试试 |
| WebUI 加群报「群号应为 5-12 位数字」 | 群号只接受数字，注意不要带空格或 `群` 字样 |
| 页面显示空白 | 插件是否启用、是否被宿主判定 WebUI 协议校验失败（查看插件加载日志） |

## 开发与测试

```bash
# 依赖：插件 SDK（含 pydantic）
pip install maibot-plugin-sdk

# 单元测试（63 个用例，全部使用假宿主，不连接 MaiBot）
python -m unittest discover -s tests -v

# 静态检查
python -m py_compile plugin.py
python -m json.tool _manifest.json > /dev/null
python -m json.tool webui.json > /dev/null
```

测试覆盖：@ 判定（`is_at` 标志 / @ 段与 `account_id` 比对 / 账号缺失）、命令解析（含 `@名片` 前缀、`/once` 不误命中）、环形缓存（截断 / LRU / 落盘往返 / 损坏恢复）、白名单（幂等 / 持久化 / 非法群号）、决策分支（私聊 / 通知 / 白名单 / 丢弃 / 注入）、注入失败降级、WebUI API 增删查清。

## 已知限制

- 不修改 MaiBot 主程序与适配器，全部通过插件 SDK 能力实现。
- 只处理群聊；私聊完全不干预。
- 不校验群管理员身份（只支持 QQ 号名单），避免依赖适配器私有接口。
- 被丢弃的消息不会进入 MaiBot 的任何历史、记忆、学习与统计。
- 缓存内容只有文本与占位符，20 条窗口是插件自己的缓存，不等同于宿主完整聊天记录。

## License

MIT
