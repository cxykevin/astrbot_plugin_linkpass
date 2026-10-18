# astrbot_plugin_linkpass

> 针对指定 UMO 的 AI 回复转义链接，避免被 QQ 群助手拦截。
> **只改目标会话里发出的文本；不改指令回复或其他会话。**

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D%204.0.0-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

## 它做什么

在消息发送前读取最终消息链，把文本中的链接改写成不可点击形式：

| 原文 | 转义后 |
| --- | --- |
| `https://example.com/a?x=1.2` | `example\.com/a\?x=1.2` |
| `example.com` | `example\.com` |
| `192.168.1.1` | `192\.168\.1\.1` |
| `2001:db8::1` | `2001\:db8\:\:1` |
| `user@example.com` | `user@example\.com` |

规则：

- 去掉 `http://` / `https://`，裸域名浏览器也能直接打开；
- 域名与 IPv4 的每个 `.` 前加 `\`；
- IPv6 的每个 `:` 前加 `\`（含 `::`、zone、IPv4-mapped 形式）；
- 邮箱只改写 `@` 后面的域名，local part 原样保留；
- query 串前的 `?` 也加 `\`，例如 `a\?x=1`；
- 路径、查询串内容、端口保持不变；
- 常见文件名（如 `notes.txt`）不会被误判成域名；
- 裸域名按常见后缀列表匹配（`com`、`net`、`org`、`top`、`cn`、`io`、`com.cn` 等），
  后缀后接 `-` 也会命中，例如 `1503dev.top-static-page` -> `1503dev\.top-static-page`；
  宁可误杀，也不放过。

## 安装与配置

```bash
cd data/plugins
git clone https://github.com/cxykevin/astrbot_plugin_linkpass.git
```

也可以直接把插件目录放进 `data/plugins/`，然后在 WebUI 的插件页重载或重启 AstrBot。

在管理面板的插件配置中填写 `target_umos`，例如：

```json
[
  "aiocqhttp:GroupMessage:123456789",
  "qq_official_webhook:GroupMessage:ABCDEF"
]
```

不知道 UMO？在目标群聊发送 AstrBot 内置的 `/sid` 指令，输出中的 `UMO:` 一行就是当前会话的 UMO。

> `target_umos` 留空时插件不会处理任何会话，这是有意的安全默认值。

### 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 关闭后插件不处理任何消息 |
| `target_umos` | list[string] | `[]` | 需要过滤的会话 UMO 列表；留空表示全部不处理 |

## 工作原理

- **非流式回复**：在 `on_decorating_result` 钩子里修改最终消息链中的 `Plain` 文本。
- **流式回复**：包装 `AstrMessageEvent.set_result`，替换 `STREAMING_RESULT` 的 `async_stream`。
  转义器只缓冲最后一个可能仍在延伸的 ASCII token，遇到空白、中文或括号就立即发出，
  因此既保持流式输出，也不会把跨 chunk 的链接拆坏。
- **直发消息**：包装 `Context.send_message`，覆盖 `send_message_to_user`、主动消息和 cron
  等不经过 `on_decorating_result` 的路径。
- **工具结果直发**：包装各平台 `AstrMessageEvent.send`，覆盖 `tool_call_result` 等
  直接调用 `event.send()` 发送的内容（webchat 的工具结果走这条路径）。
- **reasoning / think 段**也会转义，避免思考过程里展示的链接被平台识别。
- 只处理 `target_umos` 命中的会话；插件停用、卸载或重载时会自动还原所有补丁。

## 限制

- 目标会话内 `on_decorating_result` 拿到的结果都会处理（包括被标记为
  `GENERAL_RESULT` 的普通回复）；`Context.send_message` 直发文本（含主动消息）
  也会被转义。
- 转义后的链接按纯文本发送，是否能点击取决于平台；本插件的目标就是让它不被识别为链接。

## 开发者自测

在插件目录执行：

```bash
python selftest.py
```

在 AstrBot 仓库根目录也可以：

```bash
uv run python data/plugins/astrbot_plugin_linkpass/selftest.py
```

覆盖域名、IPv4、IPv6、邮箱、正文标点、流式跨 chunk、幂等性和非链接文本保护。

## License

[AGPL-3.0](LICENSE)
