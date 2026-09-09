# rxyy tools community

给 **Codex 桌面版与 Cursor** 使用的本地任务控制台。集中查看任务进度、原生输出和问题，在同一入口继续任务。MCP 统一名称为 **rxyy MCP**。

这是从 rxyy tools 提取的开源核心版，采用独立数据目录；不会读取作者的个人配置、聊天库或业务台账。当前为 Windows 优先的预发布版本。

## 包含什么

- **Codex**：原生任务绑定、增量输出、状态、问题与回答、新任务的模型/思考选择、在原任务接续。Windows 桌面 IPC 适配会核验进程和协议版本；协议不匹配时显示错误。
- **Cursor**：窗口总线扩展、创建任务、连接已有任务、原生输出和提问同步；无需购买其它扩展。
- **控制台**：多任务标签、Markdown 时间线、终态判断、团队黑板、可选工作流。
- **MCP**：`zt` 非阻塞上报，`zhi` 问答等待，`ji` 本地记忆/黑板；HTTP 和 stdio 入口。
- **手机页面**：可选令牌保护的分享页。默认仅启用本机控制台，远程分享需要自行配置。

不包含个人业务后台、远程桌面、账号密钥、自动审批绕过、商业角色图片或第三方付费插件。Codex 网页云任务不等于本机桌面任务。

## Windows 快速开始

需要 **Python 3.11 或更新版本**。Codex/Cursor 需在本机安装并由你正常登录。

从 [Releases](https://github.com/rixingyingyao/rxyy-tools-community/releases) 下载源码 ZIP，解压后在目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
.\.venv\Scripts\rxyy-tools.exe start
```

也可以克隆源码：

```powershell
git clone https://github.com/rixingyingyao/rxyy-tools-community.git
cd rxyy-tools-community
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\rxyy-tools.exe start
```

控制台打开于 `http://127.0.0.1:38777/ui`。后台进程隐藏终端，不抢输入焦点。安装本身不更改编辑器、登录状态或开机启动。

```powershell
.\.venv\Scripts\rxyy-tools.exe status
.\.venv\Scripts\rxyy-tools.exe stop
```

端口已被其它安装占用时会明确报错，不接管其它实例。用 `rxyy-tools setup` 查看配置路径，再修改 `gateway_port`、`port`、`mcp_http_port`，重启社区版即可。

## 接入 Codex

在自己的 `~/.codex/config.toml` 合并以下段落，然后重新加载 MCP 或重开应用：

```toml
[mcp_servers.rxyy-mcp]
url = "http://127.0.0.1:39222/mcp/codex"
```

控制台“新建任务”可选 Codex，并按本机实际模型目录选择模型与思考档位。已有任务可手动输入：

> 请通过 rxyy MCP 的 zt 接入此任务，runtime 使用 codex，thread_id 使用本任务的真实原生 ID。发送真实任务名与状态，默认非阻塞；需要我回答时才提问。不伪造原生任务 ID。

只有已绑定的真实任务能同步原生输出。读取/转发本地原生事件本身不调用模型；发送新消息、继续任务或启动子代理会使用相应的模型额度。

## 接入 Cursor

在 Cursor 的 MCP 配置合并：

```json
{
  "mcpServers": {
    "rxyy-mcp": {"url": "http://127.0.0.1:39222/mcp/cursor"}
  }
}
```

需要窗口发现与一键创建时，安装本项目自己的窗口总线扩展：

```powershell
.\.venv\Scripts\rxyy-tools.exe cursor-extension --install
```

安装后已开的 Cursor 窗口需执行一次 **Reload Window**；新窗口自动加载。请在没有进行中的任务时重载。扩展源码位于 `rxyy_mcp/extbus-ext/`。

## 数据与远程访问

默认数据目录为 `%LOCALAPPDATA%\rxyy-tools-community`；可用 `RXYY_MCP_DATA_DIR` 指向另一个空目录。卸载代码不会删除数据。不要提交该目录或将它打包分享。

本机控制台仅绑定回环地址。手机访问使用分享页、强随机 `share_token` 和你自己的可信 VPN/HTTPS 反向代理；不要把本机管理网关直接暴露到公网。`share_enabled` 默认为 `false`。环境变量说明可运行 `python rxyy_mcp/config_schema.py` 查看；旧 `CHIJIU_*` 名称只保留兼容。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install ".[dev]"
.\.venv\Scripts\python.exe -m pytest tests -q
```

核心适配测试位于 `rxyy_mcp/tests`；社区安装/隔离/启动测试位于 `tests`。集成测试使用临时数据和端口，不发模型消息，不改真实任务。桌面协议随 Codex、Cursor 更新可能变化；请提交应用版本、错误信息和脱敏日志，勿上传聊天数据库或登录文件。

## License

MIT。第三方静态库许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目与 OpenAI、Anysphere 无官方隶属关系。
