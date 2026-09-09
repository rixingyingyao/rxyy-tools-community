# -*- coding: utf-8 -*-
"""触碰 mcp.json 的 rxyy MCP nonce；并给工作区写项目级 /mcp/<名> URL。

此前 hub.py / server.py / 旧重启脚本 各持一份复制粘贴的实现，且全部
`except Exception: return False`——Windows 上 Cursor 自己也会读写 mcp.json，
os.replace 撞上文件锁（PermissionError）就瞬时失败，控制台却弹出误导性的
「找不到文件或无 rxyy MCP 条目」（07-27 15:10 实测红条）。统一收口：
- 重试 3 次、间隔 0.25s，扛过瞬时文件锁竞态；
- 返回 (ok, reason)，失败原因分「文件不存在 / 无 rxyy MCP 条目 / 解析失败 / 写入被锁」，
  界面不再瞎报。
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import quote


MCP_ENTRY_KEY = "rxyy MCP"
LEGACY_MCP_ENTRY_KEYS = ("持久plus", "chijiu-plus")
RELOAD_HEADER = "X-Rxyy-Mcp-Reload"
LEGACY_RELOAD_HEADER = "X-Chijiu-Reload"


def mcp_json_path():
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".cursor" / "mcp.json"


def _plus_key(servers):
    if MCP_ENTRY_KEY in servers:
        return MCP_ENTRY_KEY
    for name in LEGACY_MCP_ENTRY_KEYS:
        if name in servers:
            return name
    return next((k for k in servers if "持久" in k or "chijiu-plus" in k.lower()), None)


def project_mcp_slug(root):
    """工作区目录名 → URL 路径段。只作 Cursor 眼里的「不同 server」，hub 仍是一个。"""
    p = Path(root)
    try:
        p = p.resolve()
    except OSError:
        pass
    name = (p.name or "workspace").strip() or "workspace"
    return quote(name, safe="")


def project_mcp_url(root, port=39222):
    return "http://127.0.0.1:{}/mcp/{}".format(int(port or 39222), project_mcp_slug(root))


def _atomic_write_json(mcp_path, cfg):
    tmp = str(mcp_path) + ".tmp"
    Path(tmp).write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(tmp, mcp_path)


def ensure_workspace_mcp(root, port=39222):
    """给工作区写项目级 rxyy MCP URL，并原地迁移旧品牌条目。

    Cursor 把不同 URL 当成不同 server，跨项目接入不再挤全局那一条 /mcp。
    仍打进同一个 hub。url 已是目标值时不写盘，免得无故触发重连。
    返回 (ok, reason)，reason 为 wrote / unchanged / 失败原因。"""
    root_p = Path(root) if root else None
    if root_p is None or not root_p.is_dir():
        return False, "工作目录不存在"
    url = project_mcp_url(root_p, port)
    mcp_path = root_p / ".cursor" / "mcp.json"
    cfg = {"mcpServers": {}}
    if mcp_path.is_file():
        try:
            loaded = json.loads(mcp_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            return False, "项目 mcp.json 解析失败"
    servers = dict(cfg.get("mcpServers") or {})
    key = _plus_key(servers) or MCP_ENTRY_KEY
    entry = dict(servers.get(key) or {})
    migrating = key != MCP_ENTRY_KEY
    if entry.get("url") == url and not migrating:
        return True, "unchanged"
    entry["url"] = url
    entry.pop("command", None)
    entry.pop("args", None)
    if migrating:
        servers.pop(key, None)
    servers[MCP_ENTRY_KEY] = entry
    cfg["mcpServers"] = servers
    try:
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(mcp_path, cfg)
    except OSError as e:
        return False, "写入失败（{}）".format(e.__class__.__name__)
    return True, "wrote"


def inspect_workspace_mcp(root, port=39222):
    """只看不写。reason：ready / missing / mismatch / 失败原因。"""
    root_p = Path(root) if root else None
    if root_p is None or not root_p.is_dir():
        return False, "工作目录不存在"
    url = project_mcp_url(root_p, port)
    mcp_path = root_p / ".cursor" / "mcp.json"
    if not mcp_path.is_file():
        return True, "missing"
    try:
        loaded = json.loads(mcp_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False, "项目 mcp.json 解析失败"
    servers = dict(loaded.get("mcpServers") or {}) if isinstance(loaded, dict) else {}
    key = _plus_key(servers)
    if not key:
        return True, "missing"
    if key != MCP_ENTRY_KEY:
        return True, "legacy"
    if (servers.get(key) or {}).get("url") == url:
        return True, "ready"
    return True, "mismatch"


def _stamp_reload(entry, nonce):
    # HTTP 型条目（带 url）必须改 headers：env 是 stdio 传输才用的字段，Cursor
    # 判「配置变没变」时不看它，改它一个字都不会重连。08-07 实测有日志为证——
    # 12:36:08 与 12:39:54 两次只改 env._reload，Cursor 侧一行 createClient 都没有，
    # 全队 agent 在 Not connected 里干挂十分钟，最后是用户手点设置里的重启 MCP 才
    # 救回来；12:47:11 改 headers 的那一秒，两个窗口同时 createClient success=true。
    if entry.get("url"):
        headers = dict(entry.get("headers") or {})
        headers.pop(LEGACY_RELOAD_HEADER, None)
        headers[RELOAD_HEADER] = nonce
        entry["headers"] = headers
    env = dict(entry.get("env") or {})
    env["_reload"] = nonce   # stdio 装法仍靠它
    entry["env"] = env


def touch_mcp_json_at(mcp_path, retries=3, delay=0.25):
    """改指定 mcp.json 里 rxyy MCP 条目的 nonce。返回 (ok, reason)。"""
    mcp_path = Path(mcp_path)
    last_reason = "未知错误"
    for attempt in range(max(1, retries)):
        if attempt:
            time.sleep(delay)
        if not mcp_path.is_file():
            last_reason = "找不到 {}".format(mcp_path)
            continue  # 极小概率正撞上原子替换的空窗，重试
        try:
            # utf-8-sig 而非 utf-8：这个文件人手改过就可能带 BOM（PowerShell 5.1 的
            # `Set-Content -Encoding utf8` 就写 BOM），utf-8 读出来头上多三个字节，
            # json.loads 当场 ValueError → 走「解析失败」重试三次 → 叫醒彻底失灵，
            # 而报的原因还指向 Cursor 正在改写，查不到真凶。utf-8-sig 带不带都读得了。
            # 同一个坑 src/config.py 的 _data_dir() 已经吃过一次。
            # 注意：写回时仍写不带 BOM 的 utf-8，也就是顺手把 BOM 去掉——Cursor 自己
            # 写的本来就不带，去掉只会让下次更稳。
            cfg = json.loads(mcp_path.read_text(encoding="utf-8-sig"))
        except OSError as e:
            last_reason = "读取被占用/失败（{}），已重试".format(e.__class__.__name__)
            continue
        except ValueError:
            last_reason = "JSON 解析失败（可能正被 Cursor 改写），已重试"
            continue
        servers = cfg.get("mcpServers") or {}
        key = _plus_key(servers)
        if not key:
            return False, "mcp.json 里没有 rxyy MCP 条目"
        entry = servers[key]
        _stamp_reload(entry, str(int(time.time())))
        try:
            _atomic_write_json(mcp_path, cfg)
            return True, ""
        except OSError as e:
            # Windows: Cursor 正打开着 mcp.json 时 replace 报 PermissionError，稍等即可
            last_reason = "写入被占用（{}），已重试".format(e.__class__.__name__)
            try:
                Path(str(mcp_path) + ".tmp").unlink(missing_ok=True)
            except OSError:
                pass
            continue
    return False, last_reason


def touch_mcp_json(retries=3, delay=0.25):
    """改 ~/.cursor/mcp.json 的 rxyy MCP nonce，诱导 Cursor 重新 initialize。
    返回 (ok: bool, reason: str)。reason 在成功时为空串。"""
    return touch_mcp_json_at(mcp_json_path(), retries=retries, delay=delay)


if __name__ == "__main__":
    # 没有这段时，`rxyy-tools-community.exe --run mcp_touch.py` 只是把模块跑一遍、exit=0，
    # 看着像叫醒成功了，实际一个窗口都没动（08-07 踩过）
    import sys

    # 打包 exe 的 stdout 是 gbk/surrogateescape（它不认 PYTHONUTF8），编不出的字符
    # 抛 UnicodeEncodeError，而窗口化的 PyInstaller 程序遇未捕获异常弹的是一个看不见
    # 的模态框——进程从此永远挂着，比原来「exit=0 却什么都没干」还难查。08-07 实测：
    # 正文的中文 gbk 都编得出，只有「✓」编不出，所以正文不用符号，另加这道保险。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    _ok, _why = touch_mcp_json()
    print("已触碰 mcp.json，各 Cursor 窗口会重连 rxyy MCP" if _ok
          else "触碰失败：{}".format(_why))
    sys.exit(0 if _ok else 1)
