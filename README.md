# 苏醒吧麦麦！

> 控制麦麦为默认回复 only@，添加到白名单群聊正常回复。

MaiBot 第三方插件（`github.natsuyutori.wake-up-maimai`）。默认**只响应群里的 @ 消息**，被 @ 时把该群最近 20 条聊天记录注入 Maisaka 上下文，并**强制麦麦必须回复**（指令 + 超时兜底补发）；通过群内命令或 WebUI 页面维护白名单，白名单群内插件完全退出、麦麦按原生逻辑参与聊天。

## 行为表

| 群状态 | 普通消息（不 @） | @ 麦麦 |
|---|---|---|
| 默认（不在白名单） | **直接丢弃**：不入库、不进 Maisaka、不回复 | 注入该群最近 N 条消息并追加强制回复指令后放行；若 45 秒内还没发出消息，插件调用 Replyer 补发一条 |
| 在白名单 | 插件完全不介入，Maisaka 原生处理（可主动接话） | 插件完全不介入（**不注入**），Maisaka 原生处理 |

群内 `@麦麦 /on` 把本群加入白名单，`@麦麦 /off` 移出；WebUI 插件页可做同样的事，两边共享同一份持久化数据。

- `/on` 的回执、`/off` 的回执、以及**自动关闭**的提示，三段文案都能在配置里逐条改写（见 [群内命令](#群内命令)）。
- 可选的**自动关闭**：给 `/on` 加一个倒计时，到点自动移出白名单并往群里发一条提示；默认不关闭（见 [自动关闭](#自动关闭on-之后到点自动-off)）。

## ⚠️ 装好后请先确认这几个默认值

| 配置项 | 默认 | 为什么要看一眼 |
|---|---|---|
| `at_detection.unresolved` | `drop` | 适配器**不上报 `account_id`** 时，插件认不出「哪条 @ 是我」，会把该群的非 @ 消息当普通消息丢弃，外在表现就是「**麦麦突然不理人**」。此时日志会打 **ERROR「【需要处理】群 xxx 无法确认机器人账号」**，WebUI 页面顶部也会出现红色提示条。修法见 [排障](#排障)。 |
| `permission.mode` | `all` | 默认**任何群成员**都能 `@麦麦 /on` 把整个群加进白名单、让麦麦开始全天候接话。只想让少数人改动白名单时，把下拉框切成 `admin`，并在 `permission.admins` 里填 QQ 号。 |
| `reply.force` + `reply.fallback_enabled` | 都开启 | 被 @ 时不光要"放行"，还要求**一定有回复**。这两个开关一起决定「指令强制 + 超时兜底补发」，代价见 [强制回复](#强制回复被--时一定有回应)。 |

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
- **三段回执文案都可自定义**：`command.reply_added`（`/on` 成功）、`command.reply_removed`（手动 `/off` 成功）、`auto_off.reply_auto_closed`（到点自动关闭时发的提示）。另外 `reply_already` / `reply_not_in` / `reply_denied` 分别对应"重复 /on""不在白名单却 /off""没权限"，也都能改。
- 命令消息本身会被拦截，不会进入 Maisaka，所以麦麦不会「回答」你的命令；插件会回一条确认（可关）。
- 权限：默认 `all`，任何群成员都能切换；想收紧就把 `permission.mode` 切成 `admin`，在 `permission.admins` 里填 QQ 号（一个一行，写成 `123456` 或 `123456:昵称` 都行）。`admin` 但名单为空时会拒绝所有人并打 WARNING（防止误配成全员可操作）。旧版本的 `permission.users` 字段会自动迁移成 `admins`。

## 强制回复（被 @ 时一定有回应）

默认开启，两层机制：

1. **指令强制**：注入的聊天记录末尾会追加一段 `reply.force_note`（可改、可清空），明确要求"本轮必须调用 reply 工具回复这条 @ 消息，不允许沉默"。因为是在放行路径上做，消息仍走 MaiBot 正常流程（历史入库、记忆、工具都能用）。
2. **超时兜底**：放行后插件挂一个计时器，`reply.fallback_seconds`（默认 45 秒）内该群**一条消息都没发出**，就调用宿主 Replyer（`maisaka.reply.generate`，复用麦麦人格与表达方式）生成一条简短回复，再用 `send.text` 直接发出去。兜底内容不经过 Planner，风格/记忆比正常回复略弱；把它当成"至少不会石沉大海"的保险。

注意两点：

- 兜底是"没检测到任何出站消息"才触发，所以正常情况下不会多说话；但如果你把 `fallback_seconds` 调得很短，而麦麦恰好在那之后才回复，群里会出现两条。默认 45 秒足够覆盖正常的思考 + 打字延迟。
- `reply.force = false` 会同时关掉这两层，回到"只放行、不保证回复"。

## 自动关闭（/on 之后到点自动 /off）

在插件配置「自动关闭」里把 `auto_off.hours` 设成大于 0 的小时数（默认 `0` = **永不关闭**），之后通过 `@麦麦 /on` 加入白名单的群就会带上倒计时：

- 到点（每 30 秒检查一次）自动移出白名单，该群恢复「只回复 @」，并往群里发 `auto_off.reply_auto_closed`（默认是一句「自动回复时间到啦…」，可自定义，留空则不发）。
- 重复 `/on` 会把倒计时**重置**为从现在起算；WebUI 里手动添加的群**不会**自动关闭，需要手动移出（页面会显示「自动关闭」列，永久群显示"永不关闭"）。
- 改配置里的小时数会立即重算：以当初 `/on` 的时间为基准；把小时数改回 `0` 会清掉所有到期点，恢复"永不关闭"。
- 插件重启后到期点仍在（写在 `whitelist.json` 里）；启动瞬间已经过期的群会被静默清理（此时宿主会话可能还没建好，不保证能发出提示）。

## WebUI 页面

页面提供：

- **统计**：白名单群数、已缓存会话数、已缓存消息数、当前命令权限模式
- **权限提示**：一行文字说明当前是 `all` 还是 `admin`（含管理员人数），切模式后立刻反映
- **自动关闭提示**：一行文字说明当前是"永不自动关闭"还是"N 小时后自动关闭"
- **添加**：填群号（必填）、群名（可选）、备注（可选）→ 加入白名单
- **表格**：点击某行打开详情，可在详情里「移出白名单」（二次确认）；表格含「自动关闭」列（到期时间 + 剩余时长 / 永不关闭）
- **清空白名单**、**清空上下文缓存**（均有二次确认）

页面上的增删与群内 `/on`、`/off` 操作同一份数据，改完立即对入站消息生效，无需重载插件。

## 配置项

配置文件：`plugins/wake-up-maimai/config.toml`（首次加载生成，也可在 WebUI 配置页编辑）。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 总开关。关闭后插件完全不介入（连命令也不生效），所有群恢复原生行为 |
| `context.size` | `20` | 被 @ 时注入的最近消息条数，同时是每群缓存上限（1–50） |
| `context.prefix` | `【群聊最近 {count} 条消息（插件缓存，按时间正序）】{omitted}` | 注入内容的**提示词前缀/标题**，支持 `{count}` 与 `{omitted}` 占位符，留空用默认 |
| `context.include_bot_messages` | `false` | 机器人自己的消息是否计入缓存（多数适配器不回显，保持关闭） |
| `context.message_max_chars` | `300` | 注入文本里单条消息的截断长度 |
| `context.max_total_chars` | `4000` | 整块注入文本上限，超出时从最旧的消息开始省略 |
| `context.max_sessions` | `200` | 最多缓存多少个群，超出按最久未活跃淘汰 |
| `context.cache_ttl_hours` | `72` | **缓存自动淘汰时间**：某群超过这么多小时没有新消息，就把它的聊天缓存整体删除（后台定时执行，启动时也会先清一次）。`0` = 永不自动淘汰 |
| `context.flush_interval_seconds` | `3.0` | 缓存落盘间隔（内存中的缓存即时可用，不必等落盘） |
| `command.on_words` | `["/on"]` | 加入白名单的命令词 |
| `command.off_words` | `["/off"]` | 移出白名单的命令词 |
| `command.reply_on_toggle` | `true` | 执行命令后是否群里回执 |
| `command.reply_added` / `reply_removed` / `reply_already` / `reply_not_in` / `reply_denied` | 内置文案 | 五种回执文本，其中 `reply_added` 是 `/on` 成功、`reply_removed` 是手动 `/off` 成功时发的内容 |
| `permission.mode` | `all` | `all`=任何群成员都能切换白名单；`admin`=仅 `permission.admins` 内的 QQ 号可以 |
| `permission.admins` | `[]` | `mode=admin` 时的管理员 QQ 号列表（支持 `123456`、`123456:昵称` 两种写法） |
| `auto_off.hours` | `0` | `/on` 之后多少小时自动移出白名单并停止自动回复；`0`=永不自动关闭（0–8760，支持小数） |
| `auto_off.reply_auto_closed` | 内置文案 | 自动关闭时发到群里的提示，留空则不发 |
| `reply.force` | `true` | 被 @ 时是否在注入内容末尾追加强制回复指令 |
| `reply.force_note` | 内置文案 | 追加的强制回复指令正文，可改成你喜欢的话术，留空则不加 |
| `reply.fallback_enabled` | `true` | 是否启用超时兜底补发 |
| `reply.fallback_seconds` | `45` | 兜底等待秒数（10–300）：这么久没发出消息才补发 |
| `reply.fallback_instruction` | 内置文案 | 兜底生成时追加给 Replyer 的写作要求（默认要求简短口语化） |
| `at_detection.unresolved` | `drop` | 拿不到机器人账号时：`drop`=按非 @ 处理，`pass`=放行 |

## 数据与隐私

持久数据目录：`<MaiBot 根>/data/plugins/github.natsuyutori.wake-up-maimai/`

| 文件 | 内容 |
|---|---|
| `whitelist.json` | 白名单：群号、群名、备注、加入时间、来源（命令 / WebUI）、操作者、**自动关闭时间**、加入时的会话 ID（只用于到期提示） |
| `context_cache.json` | 每个群最近 N 条消息的**文本**，非文本消息记为 `[图片]`、`[表情]` 等占位符 |

- 只保存文本与占位符，不保存图片 / 语音 / 文件的二进制内容。
- **自动过期**：某群超过 `context.cache_ttl_hours`（默认 72 小时）没有新消息，它的缓存会在后台定时任务里被整条删除，磁盘不会无限增长。设为 `0` 可关闭自动过期，此时只能手动清。
- 两个文件都是原子写（临时文件 + `os.replace`），文件损坏时会备份为 `.bak` 并以空状态启动（日志 WARNING）。
- 卸载插件不会删除数据；需要清理时用页面「清空白名单」「清空上下文缓存」，或手动删除上述两个文件。
- 被丢弃的普通消息**不会**进入 MaiBot 的聊天记录、记忆、学习与统计——这是「丢弃」的必然结果，加入白名单后恢复正常。

## 实现要点（想改代码时看）

- **丢弃时机**：`chat.receive.before_process` 返回 `{"action": "abort"}`。宿主在 `SessionMessage.process()` 之前处理该结果并直接 `return`，因此消息既不落库也不进 Maisaka。正因如此，20 条上下文只能由插件自建缓存提供，无法从宿主数据库回读。
- **为什么不用 `@Command`**：宿主用 `processed_plain_text` 做 `search` 匹配，而 @ 段会被渲染成 `@名片` 前缀，`^/on$` 匹配不到 `@麦麦 /on`。所以命令在 Hook 内解析 `raw_message` 的文本段（先剥 @ 前缀，再整串比对，避免 `/once` 误命中）。
- **首次 @ 的会话**：群里第一条消息就是 @ 时，宿主还没有该群会话；插件先 `chat.open_session` 建流（群会话 ID 只由 platform/account_id/scope/group_id 决定，与发送者无关），再 `maisaka.context.append` 注入并重试。
- **注入失败不阻断**：注入超时 / 被策略拒绝时只打 WARNING，@ 消息照常放行，退化为「无上下文的正常回复」。
- **强制回复的两层**：提示词层只是往 `maisaka.context.append` 的文本块末尾追加指令（零风险）；兜底层用 `send_service.after_send`（观察模式 Hook）判断该会话在等待窗口内是否真的发出了消息——发出就清令牌取消兜底，没发出才调 `maisaka.reply.generate` + `send.text` 补一条。
- **自动关闭**：到期时间写在白名单记录里（`expires_at` + 加入时的 `session_id`），后台每 30 秒扫一次，到点移除并发提示；启动时静默清理已过期记录。只有 `/on` 命令来源的记录会带到期时间，WebUI 手动添加的视为长期有效。
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
11. 把 `context.cache_ttl_hours` 临时改成 `0` 之外的小值（如 `1`）并等待后台任务，日志出现「上下文缓存按活跃度淘汰」，页面「缓存会话数」下降（验证自动过期）。
12. 把 `permission.mode` 切成 `admin`、`permission.admins` 只填自己：自己 `@麦麦 /off` 生效，换个小号再试会被拒绝并收到「没有权限」回执（页面顶部提示同步变成 admin 文案）。
13. 兜底验证：临时把 `reply.fallback_seconds` 调到 `10`、并在 MaiBot 里让该群回复频率为 0（或直接不回复），`@麦麦 在吗` 后约 10 秒，日志出现「已由插件调用 Replyer 补发一条回复」，群里收到一条回复。
14. 把 `context.prefix` 改成 `【本群最近 {count} 条】` 后 `@麦麦`，在 Maisaka 上下文里能看到自定义前缀；`reply.force_note` 改动同样生效。
15. 自动关闭：`auto_off.hours` 设成 `0.05`（3 分钟）→ `@麦麦 /on`（页面「自动关闭」列显示还剩几分钟）→ 等 3 分钟（检查间隔 30 秒，最多 3.5 分钟）→ 群里收到 `auto_off.reply_auto_closed` 那条提示，之后普通消息不再被回复，页面该群消失。
16. 三段文案自定义：把 `command.reply_added`、`command.reply_removed`、`auto_off.reply_auto_closed` 都改成好认的句子，分别走一遍 `/on`、`/off`、自动到期，确认群里收到的是自己改的文案。
17. `auto_off.hours` 从 `3` 改成 `1` 并保存配置：日志出现「重算 N 个群」，页面「自动关闭」列的剩余时间随之变短；改回 `0` 后该列全部变回「永不关闭」。

## 排障

| 现象 | 排查 |
|---|---|
| 机器人完全不回复任何消息 | 插件 `enabled` 是否为 true；该群是否在白名单（白名单群不走本插件，但 MaiBot 本身也可能因频率/策略不回复）；看日志有没有「无法确认机器人账号」 |
| @ 了也不回复 | 日志 **ERROR「【需要处理】群 xxx 无法确认机器人账号」** 说明 `account_id` 缺失或不一致：核对 `bot_config.toml` 的 `qq_account` 与 SnowLuma 里登录的 QQ；WebUI 页面顶部也会出现同一条红色提示，列出受影响的群。也可临时把 `at_detection.unresolved` 设为 `pass` 验证 |
| 命令没反应 | 是否 @ 了机器人；`command.on_words` 是否被改；`permission.mode=admin` 时自己是否在 `permission.admins` 里（日志会写「权限不足」，群里会收到无权限回执） |
| 被 @ 了但还是没回复 | 先看日志有没有「已由插件调用 Replyer 补发一条回复」：有说明兜底生效了；没有则检查 `reply.force` / `reply.fallback_enabled` 是否被关、宿主是否支持 `maisaka.reply.generate` 能力（MaiBot 1.3.x 自带）；若两处都正常仍无回复，再看 `chat.reply_timing.talk_value` 是否被设为 0 |
| 同一个 @ 收到两条回复 | 兜底超时时间太短，而麦麦在那之后才发出正常回复。把 `reply.fallback_seconds` 调大（默认 45 秒），或关掉 `reply.fallback_enabled` 只用指令强制 |
| 被 @ 有回复但没有历史上下文 | 日志有「上下文注入失败」：首次建流失败或适配器策略拒绝；群里先随便聊几条再 @ 试试 |
| WebUI 加群报「群号应为 5-12 位数字」 | 群号只接受数字，注意不要带空格或 `群` 字样 |
| 页面显示空白 | 插件是否启用、是否被宿主判定 WebUI 协议校验失败（查看插件加载日志） |

## 开发与测试

```bash
# 依赖：插件 SDK（含 pydantic）
pip install maibot-plugin-sdk

# 单元测试（94 个用例，全部使用假宿主，不连接 MaiBot）
python -m unittest discover -s tests -v

# 静态检查
python -m py_compile plugin.py
python -m json.tool _manifest.json > /dev/null
python -m json.tool webui.json > /dev/null
```

测试覆盖：@ 判定（`is_at` 标志 / @ 段与 `account_id` 比对 / 账号缺失）、命令解析（含 `@名片` 前缀、`/once` 不误命中）、环形缓存（截断 / LRU / 落盘往返 / 损坏恢复 / TTL 淘汰）、白名单（幂等 / 持久化 / 非法群号 / 到期查询）、自动关闭（到期移除并提示 / 无会话 ID 跳过提示 / 配置变更重算 / WebUI 添加不过期）、决策分支（私聊 / 通知 / 白名单 / 丢弃 / 注入）、注入失败降级、强制回复（指令注入 / 兜底触发 / 已回复则取消 / 生成失败不外发）、权限模式（all / admin / 旧 `users` 字段迁移）、WebUI API 增删查清。

## 已知限制

- 不修改 MaiBot 主程序与适配器，全部通过插件 SDK 能力实现。
- 只处理群聊；私聊完全不干预。
- 不校验群管理员身份（只支持 QQ 号名单），避免依赖适配器私有接口。
- 被丢弃的消息不会进入 MaiBot 的任何历史、记忆、学习与统计。
- 缓存内容只有文本与占位符，20 条窗口是插件自己的缓存，不等同于宿主完整聊天记录。
- 兜底回复由 `maisaka.reply.generate`（Replyer 长文生成链路）产出，**不经过 Planner**，因此不会调用工具、也不会走正常 Agent 记忆检索；它只是保证"有回应"的保险，正常回复仍以 Planner 为主。
- 自动关闭按 30 秒粒度检查，所以实际关闭时间可能比设定值晚最多 30 秒；重启瞬间已过期的群会被静默清理（不保证发出关闭提示）。

## 更新日志

### 1.3.0
- 新增「自动关闭」：`auto_off.hours`（默认 `0` = 永不关闭）设成大于 0 后，`@麦麦 /on` 加入的群到点自动移出白名单并停止自动回复，同时向该群发送 `auto_off.reply_auto_closed`。支持配置热更新重算、重启后保留到期点；WebUI 手动添加的群不参与自动关闭。
- 三段回执文案全部可自定义：`command.reply_added`（`/on`）、`command.reply_removed`（手动 `/off`）、`auto_off.reply_auto_closed`（自动关闭）。
- WebUI 白名单表格新增「自动关闭」列（到期时间 + 剩余时长 / 永不关闭），详情弹窗同步展示，页面顶部显示当前自动关闭策略。

### 1.2.0
- 新增 `permission.mode` 的 `all` / `admin` 两种模式与 `permission.admins` 管理员名单：默认 `all`（任何群成员可切换白名单），切成 `admin` 后只有名单内 QQ 号能执行 `@麦麦 /on`、`/off`；旧版 `users` 字段自动迁移。
- 新增 `context.prefix`：注入给 Maisaka 的提示词前缀可配置（支持 `{count}` / `{omitted}` 占位符）。
- 新增「强制回复」：`reply.force` 在注入内容末尾追加可配置的「必须调用 reply 工具」指令；`reply.fallback_enabled` + `reply.fallback_seconds` 在超时未回复时调用 Replyer 生成并补发一条，确保 @ 一定有回应。
- WebUI 页面新增「命令权限」统计与权限模式说明，折叠说明区补充强制回复机制。

### 1.1.0
- 新增 `context.cache_ttl_hours`（默认 72 小时）：按活跃度自动淘汰长期没有新消息的群缓存，后台定时执行，启动时也会先清理一次；`context_cache.json` 不再无限增长，设 `0` 可关闭。
- 「认不出机器人身份」的告警从 WARNING 升级为 **ERROR**，并在 WebUI 页面顶部显示提示条（列出受影响的群与修法），避免出现「麦麦突然不理人」却查不到原因。
- README 新增「装好后请先确认这两个默认值」，说明 `at_detection.unresolved` 与 `permission.mode` 的影响。

### 1.0.0
- 首个版本：默认只回复 @、@ 时注入最近 20 条群聊上下文、`/on` `/off` 与 WebUI 白名单管理。

## License

MIT
