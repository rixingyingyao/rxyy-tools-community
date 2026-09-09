# rxyy MCP · 窗口总线

让rxyy MCP 的 hub（独立进程）能在这个 Cursor 窗口里做两件只有扩展宿主才做得到的事：

- **无头开新对话**（`composer.createNew`，不抢焦点、不敲键盘）——控制台「一键开 N 个待命对话」「新开 Cursor 对话接手」靠它
- **把某个对话调到前台**（`aichat.openAgentById` / `composer.openComposer`）

原理：每个窗口把自己登记到 `%LOCALAPPDATA%\rxyy-tools-community\extbus\instances.json`，
并每秒泵一次 `cmds\<instanceId>\*.req.json`，执行后把结果写回 `.res.json`。
hub 侧见 `rxyy_mcp/ext_bus.py`。

不改 Cursor 任何安装文件、不写工作区规则、不联网。

打包 / 安装：`py -3.11 rxyy_mcp\extbus_build.py --install`（已开着的窗口要 Reload Window 才加载）。
