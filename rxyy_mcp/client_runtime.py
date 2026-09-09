# -*- coding: utf-8 -*-
"""rxyy MCP 的 MCP 客户端运行时与 HTTP 会话适配。

这一层只保存「哪个 MCP 客户端在说话」以及它的协议握手信息。它不依赖 hub，
也不把客户端身份写入全局单例；HTTP 用 ``Mcp-Session-Id``，没有该头的老客户
端退回到当前 HTTP 连接，stdio 则由调用方持有一份独立的 RuntimeContext。
"""

from dataclasses import dataclass, field
import threading
import time
import uuid
from urllib.parse import urlsplit


RUNTIME_KINDS = ("cursor", "codex", "chatgpt", "unknown")
NATIVE_RUNTIME_KINDS = frozenset(("codex", "chatgpt"))
SESSION_HEADER = "Mcp-Session-Id"

# Codex 的 initialize.instructions 前半段会被截断；这段文本本身就包含工具
# 的最小使用方式，不依赖 Cursor 的工具名、侧栏动作或永续保活纪律。
NATIVE_INSTRUCTIONS = (
    "rxyy MCP：遵守宿主与用户指令，按需使用工具。"
    "zhi 用于向用户提问或汇报；默认 wait=false 只发不等；明确 wait=true 最多等待45秒，"
    "未回复时提问仍保留，可稍后再次调用领取。"
    "zt 是非阻塞状态上报，ji 按需用于记忆、转告或黑板。"
    "调用可带 runtime、thread_id、conversation_id、project_path；同一对话复用 conversation_id。"
)
UNKNOWN_INSTRUCTIONS = (
    "rxyy MCP：遵守宿主与用户指令，按需使用工具。"
    "zhi 用于向用户提问或汇报；用 wait=false 只发不等，用 wait=true 等待回复，"
    "未回复时提问仍保留，可稍后再次调用领取。"
    "zt 是非阻塞状态上报，ji 按需用于记忆、转告或黑板。"
    "调用可带 runtime、thread_id、conversation_id、project_path；同一对话复用 conversation_id。"
)


def _clean(value):
    return str(value or "").strip()


def normalize_runtime(value):
    """把调用方/客户端的运行时名称收敛到公开的四值枚举。"""
    raw = _clean(value).lower().replace("_", "-").replace(" ", "-")
    if raw in RUNTIME_KINDS:
        return raw
    aliases = {
        "cursor-ide": "cursor",
        "cursor-vscode": "cursor",
        "cursor-editor": "cursor",
        "openai-codex": "codex",
        "codex-cli": "codex",
        "codex-app": "codex",
        "openai-chatgpt": "chatgpt",
        "chat-gpt": "chatgpt",
        "chatgpt-web": "chatgpt",
        "chatgpt-app": "chatgpt",
    }
    if raw in aliases:
        return aliases[raw]
    # clientInfo 的 name 往往带版本或 transport 后缀，保守地按明显前缀识别。
    if raw.startswith("cursor-") or raw.startswith("cursor/"):
        return "cursor"
    if raw.startswith("codex-") or raw.startswith("codex/"):
        return "codex"
    if raw.startswith("chatgpt-") or raw.startswith("chatgpt/"):
        return "chatgpt"
    return "unknown"


def infer_runtime(client_info=None, profile=None):
    """从显式 profile 或 initialize.clientInfo 推断运行时。

    ``/mcp/codex`` / ``/mcp/chatgpt`` 是远端客户端无法可靠填写 clientInfo 时的
    兜底，因此显式 native profile 优先于握手名称；普通 ``/mcp`` 不猜测。
    """
    profile_kind = normalize_runtime(profile)
    if profile_kind in RUNTIME_KINDS and profile_kind != "unknown":
        return profile_kind
    info = client_info if isinstance(client_info, dict) else {}
    for value in (info.get("name"), info.get("client"), info.get("runtime")):
        kind = normalize_runtime(value)
        if kind != "unknown":
            return kind
    return "unknown"


def is_native(runtime_kind):
    return normalize_runtime(runtime_kind) in NATIVE_RUNTIME_KINDS


def instructions_for(runtime_kind, disabled=False):
    """返回 Codex/ChatGPT/未知客户端可安全接收的短说明。"""
    if disabled:
        return "rxyy MCP 当前处于禁用模式；遵守宿主与用户指令，按需使用工具。"
    return NATIVE_INSTRUCTIONS if is_native(runtime_kind) else UNKNOWN_INSTRUCTIONS


def header_value(headers, name):
    """兼容 ``HTTPMessage``、普通 dict 和大小写不同的测试 headers。"""
    if headers is None:
        return ""
    try:
        got = headers.get(name)
        if got:
            return _clean(got)
    except Exception:
        pass
    wanted = name.lower()
    try:
        for key, value in headers.items():
            if _clean(key).lower() == wanted:
                return _clean(value)
    except Exception:
        pass
    return ""


def profile_from_path(path):
    """只把明确的 native URL 后缀当 profile，普通项目后缀不做猜测。"""
    parsed = urlsplit(_clean(path) or "/")
    value = parsed.path.rstrip("/").lower()
    if value == "/mcp/codex":
        return "codex"
    if value == "/mcp/chatgpt":
        return "chatgpt"
    if value in ("", "/mcp"):
        return ""
    return ""


def _new_session_id():
    return "chijiu-" + uuid.uuid4().hex


@dataclass
class RuntimeContext:
    """一次 MCP 连接/会话的握手状态。"""

    runtime_kind: str = "unknown"
    client_name: str = ""
    client_version: str = ""
    protocol_version: str = "2024-11-05"
    capabilities: dict = field(default_factory=dict)
    profile: str = ""
    connection_id: str = ""
    session_id: str = ""
    advertise_session: bool = False
    initialized: bool = False
    root_path: str = ""

    @property
    def scope(self):
        """SSE/rpc 取消的隔离键；无 session 的旧客户端仍按连接隔离。"""
        if self.session_id:
            return "http-session:" + self.session_id
        if self.connection_id:
            return "http-connection:" + self.connection_id
        return ""

    def apply_initialize(self, params=None, profile=None, session_id=None):
        params = params if isinstance(params, dict) else {}
        info = params.get("clientInfo") or {}
        if not isinstance(info, dict):
            info = {}
        profile_kind = normalize_runtime(profile)
        self.profile = profile_kind if profile_kind != "unknown" else ""
        self.runtime_kind = infer_runtime(info, profile)
        self.client_name = _clean(info.get("name"))
        self.client_version = _clean(info.get("version"))
        self.protocol_version = _clean(params.get("protocolVersion")) or "2024-11-05"
        caps = params.get("capabilities") or {}
        self.capabilities = dict(caps) if isinstance(caps, dict) else {}
        if session_id is not None:
            self.session_id = _clean(session_id)
        # Native endpoints/clients receive a session id so later requests can be
        # routed even when they arrive on a different HTTP connection. Cursor on
        # the legacy /mcp path remains sessionless unless it already sent one.
        self.advertise_session = bool(
            self.session_id or self.runtime_kind in NATIVE_RUNTIME_KINDS
            or self.profile in NATIVE_RUNTIME_KINDS
        )
        self.initialized = True
        return self


class HttpRuntimeRegistry:
    """线程安全的 HTTP session → RuntimeContext 映射。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = {}
        self._connections = {}
        self._seen = {}

    def _prune_locked(self):
        now = time.monotonic()
        expired = {sid for sid, ts in self._seen.items() if now - ts > 86400}
        overflow = max(0, len(self._sessions) - 511)
        expired.update(sorted(self._seen, key=self._seen.get)[:overflow])
        for sid in expired:
            self._sessions.pop(sid, None)
            self._seen.pop(sid, None)
        for key, context in list(self._connections.items()):
            if context.session_id in expired:
                self._connections.pop(key, None)

    @staticmethod
    def _connection_key(connection_key):
        return str(connection_key or "") or "anonymous"

    def initialize(self, connection_key, params=None, profile=None,
                   incoming_session_id=None):
        key = self._connection_key(connection_key)
        sid = _clean(incoming_session_id)
        with self._lock:
            self._prune_locked()
            # A new initialize must not mutate a context held by an in-flight
            # request from the previous client, even on the same TCP connection.
            context = RuntimeContext(connection_id=key)
            context.apply_initialize(params, profile=profile, session_id=sid or None)
            issued = False
            if context.advertise_session and not context.session_id:
                context.session_id = _new_session_id()
                issued = True
            self._connections[key] = context
            if context.session_id:
                self._sessions[context.session_id] = context
                self._seen[context.session_id] = time.monotonic()
            return context, issued

    def resolve(self, connection_key, headers=None, profile=None):
        key = self._connection_key(connection_key)
        sid = header_value(headers, SESSION_HEADER)
        with self._lock:
            self._prune_locked()
            if sid:
                context = self._sessions.get(sid)
                if context is None:
                    # A request can race with initialize on another transport. Keep
                    # the caller-provided id isolated, but do not borrow another
                    # client's last handshake.
                    context = RuntimeContext(
                        runtime_kind=infer_runtime({}, profile),
                        profile=normalize_runtime(profile),
                        connection_id=key,
                        session_id=sid,
                        advertise_session=True,
                    )
                    self._sessions[sid] = context
                else:
                    context.connection_id = key
                self._connections[key] = context
                self._seen[sid] = time.monotonic()
                return context
            context = self._connections.get(key)
            if context is not None:
                return context
            context = RuntimeContext(
                runtime_kind=infer_runtime({}, profile),
                profile=normalize_runtime(profile),
                connection_id=key,
            )
            self._connections[key] = context
            return context

    def remove(self, connection_key, session_id=None):
        key = self._connection_key(connection_key)
        sid = _clean(session_id)
        with self._lock:
            context = self._connections.pop(key, None)
            if sid:
                self._sessions.pop(sid, None)
                self._seen.pop(sid, None)
            elif context is not None and context.session_id:
                self._sessions.pop(context.session_id, None)
                self._seen.pop(context.session_id, None)

    def release_connection(self, connection_key):
        """HTTP 连接关闭时只释放连接索引，保留可跨连接恢复的 session。"""
        key = self._connection_key(connection_key)
        with self._lock:
            self._connections.pop(key, None)

    def clear(self):
        with self._lock:
            self._sessions.clear()
            self._connections.clear()
            self._seen.clear()
