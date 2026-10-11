# Codex Monitor

![Codex Monitor](assets/codex-monitor-hero.png)

[English](README.md) | 中文

> 本 fork 基于 [KevinKE93/Codex-Monitor](https://github.com/KevinKE93/Codex-Monitor)，保留原作者及 MIT 许可。v0.4.0 增加完整轮次计数、压缩用量、工具调用及跨重置周期的周额度分摊估算。代码和合成页面已验收，尚未向真实客户端安装或注入。

- **完整一轮**：合并该轮全部模型调用；单独显示已计入总数的压缩 tokens。按响应标识去重，不重复添加镜像计数。
- **工具调用**：本轮/会话次数、工具名称及已返回次数；返回不代表执行成功，不展示参数或结果正文。
- **周额度**：本周期百分比、各周期明细、跨周期累计百分点，均标明估算；显示本机 token 覆盖率和缺口。累计可超过 100 个百分点，不能理解为本周百分比。
- **历史显示**：每轮最后一条助手消息承载整轮用量；移除 40 条截断与按位置猜配，聊天身份不明确时不贴标签。

详见 [统计口径与限制](docs/ACCOUNTING.md) 和 [验收记录](docs/VALIDATION.md)。

Codex Monitor 是一个本地只读的 Codex 监控浮层，同时支持当前集成在 ChatGPT 桌面客户端里的 Codex 和旧版独立 Codex Desktop。它可以在现有窗口中显示 context window 使用情况和 token 消耗，不修改客户端安装包，也不会把本地会话数据复制进仓库。

![Codex Monitor demo](assets/codex-monitor-demo.svg)

## 作者

由 Kevin KE 制作。

- GitHub: [KevinKE93](https://github.com/KevinKE93)

## 主要功能

- 在 Codex Desktop 内显示可拖动的 `Monitor` 面板。
- Monitor 收起时在标题里直接显示当前 session 总 token，例如 `Monitor (ttk:58.9M)`。
- 每条回复的原生操作按钮下方显示独立整行 Token 信息，窄窗口下可以自动换行。
- 左侧会话列表 hover 面板显示当前 session 的 total、input、cached input、output 和 reasoning token。
- Codex 界面语言为中文时自动使用中文标签；英语及其他语言统一使用英文。
- token 单位支持 raw、K、M，默认使用 K。
- Monitor 收起后只保留紧凑标题和展开按钮，隐藏单位切换控件。
- 通过 Chrome DevTools Protocol 本地运行，不依赖远端服务。
- 自动识别当前 `/Applications/ChatGPT.app` 和旧版 `/Applications/Codex.app`。
- 同时兼容 Codex task turn 与集成版 ChatGPT conversation turn 的消息节点。

## 安装

复制这个 GitHub URL：

```text
https://github.com/Lywooye/Codex-Monitor
```

在 Codex Desktop 里打开 plugin 或 marketplace 安装入口，粘贴这个 URL。也可以用 CLI 安装：

```bash
codex plugin marketplace add https://github.com/Lywooye/Codex-Monitor --ref main
codex plugin add codex-monitor@codex-monitor
```

安装后，对 Codex 说：

```text
Start Codex Monitor.
```

plugin 安装会让 `codex-monitor` skill 可用。可视化浮层仍然需要启动本地 injector。若希望登录后、Codex 重启后、Codex 更新后自动启动，请在安装后的插件根目录运行：

```bash
./scripts/install_launch_agent.sh 9222
```

LaunchAgent 会在 Codex 关闭时等待，不会在用户正常退出 Codex 后自动重新打开 Codex。

## 使用方式

推荐使用自动重注入模式：

```bash
./scripts/start_codex_monitor.sh 9222
```

这是推荐方式。脚本会用本地 DevTools 端口打开 Codex，并持续运行 injector；当 Codex renderer 重启后，浮层会自动恢复。
injector 默认每 10 秒刷新一次 session payload；普通的页面切换和 DOM 更新由页面内 observer 处理。
为了减少卡顿，侧边栏 hover 的 summary 默认覆盖最近 100 个 session；每轮 chip 详情默认解析最近 6 个 session，并额外解析当前选中的旧会话。额度分摊的分母始终包含所有能读取的本地会话，不受这些显示数量限制。首次扫描完整历史，随后增量读取。
长会话详情会保留缓存，后续只解析 JSONL 新追加的行，不会每次刷新都从头读取整个会话。
如果请求的端口被占用，脚本只会在该端口确实属于 Codex renderer 时复用；否则自动切到下一个可用的本地端口。

安装 macOS LaunchAgent，让 Monitor 在登录后、Codex 重启后、Codex 更新后自动拉起：

```bash
./scripts/install_launch_agent.sh 9222
```

在 LaunchAgent 模式下，Monitor 会等待你再次打开 Codex，而不是在你退出 Codex 后强制重启它。

停止自动启动：

```bash
./scripts/uninstall_launch_agent.sh
```

手动用本地 DevTools 端口启动 Codex：

```bash
./scripts/reopen_codex_with_debug.sh 9222
```

向当前 Codex 窗口注入 Monitor：

```bash
./run_once.sh 9222
```

Codex 重启后需要重新运行。当前页面保持打开时，已注入的 UI 会持续更新。

## Codex 客户端升级处理

Codex Monitor 注入的是临时 DOM 元素，这是有意设计的：它避免修改 `ChatGPT.app`、`Codex.app` 或客户端资源。如果桌面客户端升级、重启或替换 renderer，已注入的 UI 会消失，需要重新注入。

自动化方式请使用 `./scripts/start_codex_monitor.sh 9222`。它会自动识别两个受支持的 App，用仅绑定本机的 DevTools 端口重新启动，并在 renderer 被替换后重连。使用 `./scripts/install_launch_agent.sh 9222` 可以让这个循环在登录后和客户端更新后持续运行。Codex 被用户主动关闭时 LaunchAgent 只等待；你普通启动客户端后，可能会短暂重启一次以补上 DevTools 参数。若当前正在生成回复，客户端可以把这次重启延后到下次启动。

## 更新日志

### v0.4.0 (fork)

- 完整轮次/压缩/工具统计、按额度重置周期分摊、覆盖缺口与保守匹配。
- 流式建立完整历史索引，后续增量读取；不新增第三方运行依赖。

### v0.3.2

- 多步骤任务按原生回复操作按钮行绑定唯一 Token 信息，避免重复追加。
- 刷新时自动清理历史遗留信息，保持对话高度稳定，切换任务后可正常滚动到底部。

### v0.3.1

- 将每条回复的 Token 信息移动到原生操作按钮下方，使用可换行的独立整行布局。
- 根据 Codex 界面语言自动切换中文或英文标签。

### v0.3.0

- 支持当前新版 ChatGPT 桌面客户端中的 Codex，已在 `ChatGPT.app` `26.707.72221`（build `5307`）完成验证，并继续兼容旧版独立 Codex 客户端。
- 优化会话切换和数据刷新性能，通过更可靠的当前任务识别、增量解析和稳定的界面更新减少卡顿。

## Codex 插件

这个仓库也已经封装成 Codex plugin：

- 插件清单：`.codex-plugin/plugin.json`
- 插件技能：`skills/codex-monitor/SKILL.md`
- GitHub 安装用 marketplace 清单：`.agents/plugins/marketplace.json`

插件暴露了本地脚本和使用流程。当前 Codex plugin 还没有提供受支持的原生 sidebar 或消息 DOM render hook，因此可视化浮层仍然通过本地 DevTools injector 按需启用。

## 命令行查看

```bash
python3 ./scripts/context_token_inspector.py --latest --format footer
python3 ./scripts/context_token_inspector.py --latest --format hover
python3 ./scripts/context_token_inspector.py --limit 20 --format table
```

## 显示名词解释

| 名词 | 含义 | 来源或计算方式 |
| --- | --- | --- |
| `context` | 当前请求占用的上下文量。 | `last_token_usage.input_tokens` |
| `context window` | Codex token-count 事件里记录的模型上下文窗口上限。 | `model_context_window` |
| `left` | 当前请求预计剩余 context。 | `context window - context` |
| `Token: Current` | 当前请求 context 占用量与模型 context window 的比例。 | `last_token_usage.input_tokens / model_context_window` |
| `Token: Total` | 完整一轮 / 当前会话总 tokens（均包含已记录的压缩）。 | 去重后的原生 usage 求和；旧格式用累计差值 |
| `session` | Monitor 面板中的当前 session 总消耗，不统计其他会话。 | 最新 session JSONL |
| `in` | Codex 记录的输入 token。在 Monitor 的 session 行里，它表示当前 session 累计输入。 | `input_tokens` |
| `cached` | Codex 记录的 cached input token。当重复上下文被复用时，这个值可能较高。 | `cached_input_tokens` |
| `out` | assistant 生成的输出 token。 | `output_tokens` |
| `reason` | Codex 记录的 reasoning output token。 | `reasoning_output_tokens` |
| `user rounds` | 有用量或工具记录的完整任务轮次。旧格式以用户消息分轮。 | 原生 turn_id / 旧格式用户消息 |
| `assistant rounds` | 有可匹配助手消息的轮次；进度消息不会重复显示整轮消耗。 | 每轮最后一条助手消息 |
| `status` | context 压力提示。低于 70% 为 `OK`，达到 70% 为 `WATCH`，达到 85% 为 `HIGH`。 | `context / context window` |
| `raw / K / M` | token 显示单位。`raw` 显示原始整数，`K` 显示千，`M` 显示百万。默认单位是 `K`。 | UI 设置 |

## 安全边界

Codex Monitor 不会修改：

- `ChatGPT.app`
- `Codex.app`
- `app.asar`
- Codex 会话 JSONL 文件
- Codex 设置或登录认证信息

它只读取本地 Codex session 日志，并向带本地 DevTools 端口启动的 Codex renderer 注入临时 DOM 元素。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

## 仓库隐私

本仓库只包含源码、测试和合成示意图，不包含本地 Codex session 数据、生成日志、marketplace 元数据、私人对话截图或对话记录。

## 开源许可

MIT 许可证。详见 [LICENSE](LICENSE)。
