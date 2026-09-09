# -*- coding: utf-8 -*-
"""rxyy MCP 配置/凭证分离层（开源产品化蓝图 阶段2「配置/凭证分离」）。

## 为什么有这层

现状（开源硬伤）：配置项散在 hub.DEFAULTS 里靠注释说明，凭证（分享令牌、Bark
推送 key、Cursor API key）跟普通配置一起躺在 config.json，别人拿到手既不知道
哪些必须填、也没法用环境变量把密钥喂进来而不落盘。

这层**只做叠加，不改 hub 现有读写**：
- `SCHEMA`：开源用户真正要配的那几项的单一真源（默认值/是否敏感/env 变量名/
  是否必填/人话说明）。hub.DEFAULTS 仍是运行时默认值的权威；本表是它的「对外
  说明书」子集，靠 test_config_schema 的一致性断言锁住不漂移。
- `apply_env_overrides(cfg)`：在 load_config 末尾叠一层——对应 env 变量存在才
  覆盖，按默认值类型转换。**不设任何 env 变量时，cfg 逐字节不变**（零风险接入）。
- `redact(cfg)`：返回脱敏副本，敏感项打码，供日志/导出/分享用（不改原 dict）。
- `example_config()` / `render_example_json()`：生成 config.example.json 模板，
  敏感项留空并指向对应 env 变量——干净机器照着填即可。
- `missing_required(cfg)`：首次运行向导用，列出「要对外用但还没配」的项。
- `python config_schema.py`：首次运行向导，把 example 写到 DATA_DIR/config.example.json
  并打印缺配引导。不接入 hub 启动流程，跑不跑都不影响现有运行。

本模块零 hub 依赖（apply/redact/example 全是纯函数，参数进参数出），故 hub.py
在 load_config 里 `import config_schema` 不会造成循环导入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigField:
    """一个对外配置项的说明。

    - key：config.json 里的键名（必须与 hub.DEFAULTS 对齐，一致性测试锁死）。
    - default：开源默认值（与 hub.DEFAULTS[key] 相同；本表只覆盖对外子集）。
    - secret：是否凭证/密钥——决定 redact 打码、example 留空、不建议落盘。
    - env：环境变量名；设了它就用它覆盖 config.json（凭证可只走 env 不落盘）。
    - required：要「对外提供服务」是否必须配（如手机远程访问必须填 remote_base_url）。
      纯本机自用可不填，故向导只提示、不强制。
    - desc：人话说明，进 example 注释。
    """

    key: str
    default: object
    secret: bool
    env: str
    required: bool
    desc: str


# 对外配置说明书（单一真源）。只收开源用户真正要碰的项；auto_reload_* / detach_* /
# team_* 等内部调优与运行态不进这里（它们仍由 hub.DEFAULTS 兜底、面板/config.json 调）。
SCHEMA: tuple[ConfigField, ...] = (
    ConfigField(
        "remote_base_url", "", False, "RXYY_MCP_REMOTE_BASE_URL", True,
        "远程访问基址 http://host:port（Tailscale/公网可达）；手机不在本机局域网时必填，"
        "二维码与分享链接用它。留空则用探测到的局域网 IP。"),
    ConfigField(
        "share_https_base", "", False, "RXYY_MCP_SHARE_HTTPS_BASE", False,
        "分享页 https 基址（有自建域名/反代时填，如 https://plus.example.com）。"),
    ConfigField(
        "bark_url", "", True, "RXYY_MCP_BARK_URL", False,
        "手机推送地址（Bark/ntfy，含 token）：AI 发起提问时推到手机。留空关闭。属凭证，"
        "建议走 env 不落盘。"),
    ConfigField(
        "push_enabled", False, False, "RXYY_MCP_PUSH_ENABLED", False,
        "是否开启手机推送（需同时配 bark_url）。"),
    ConfigField(
        "share_token", "", True, "RXYY_MCP_SHARE_TOKEN", False,
        "分享页访问令牌。留空则 hub 首次启动自动生成随机值；属凭证，泄露=分享页被陌生人访问。"),
    ConfigField(
        "mcp_http_port", 39222, False, "RXYY_MCP_MCP_HTTP_PORT", False,
        "MCP Streamable HTTP 守护端口（0=关）。mcp.json 用 url 接入后由 hub 常驻拉起。"),
    ConfigField(
        "agent_backend", "cli", False, "RXYY_MCP_AGENT_BACKEND", False,
        "「+ 拉起本机 agent」后端：cli=Cursor CLI 走订阅额度；sdk=cursor-sdk 走 API 计费。"),
    ConfigField(
        "cursor_api_key", "", True, "RXYY_MCP_CURSOR_API_KEY", False,
        "agent_backend=sdk 时的 Cursor API key（cursor.com/dashboard → Integrations 生成）。"
        "属凭证，建议走 env 不落盘。"),
    ConfigField(
        "keepalive_secs", 600, False, "RXYY_MCP_KEEPALIVE_SECS", False,
        "zhi 保活轮询秒数（默认 600）。推荐设 0=纯 SSE 长挂（progress 心跳撑住超时钟）；"
        "勿设小正值——会退回轮询 detach，易误判失联漏读回复（见 docs/zhi投递-keepalive纯SSE长挂）。"),
)

# secret / env 派生集合（从 SCHEMA 单点导出，别处引用不再各写一份）。
SECRET_KEYS: frozenset[str] = frozenset(f.key for f in SCHEMA if f.secret)
ENV_MAP: dict[str, str] = {f.env: f.key for f in SCHEMA if f.env}
# 旧环境变量属于部署协议，升级读取但不再写入示例或主动提示。
LEGACY_ENV_MAP: dict[str, str] = {
    env_name.replace("RXYY_MCP_", "CHIJIU_", 1): key
    for env_name, key in ENV_MAP.items()
}
_BY_KEY: dict[str, ConfigField] = {f.key: f for f in SCHEMA}


_INVALID = object()  # _coerce 的「解析失败」哨兵：叠加层宁可不动 cfg，也不硬塞默认值


def _coerce(value: str, like) -> object:
    """把 env 字符串按参照默认值的类型转换。bool 认 1/true/yes/on（大小写不敏感）。

    int 解析失败返回 _INVALID（4b0a9d81 互审建议）：env 是「叠加」语义，非法值
    盖成 SCHEMA 默认会把 config.json 里的用户现值顶掉——正确做法是当它没设。
    """
    if isinstance(like, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        try:
            return int(value.strip())
        except ValueError:
            return _INVALID
    return value


def apply_env_overrides(cfg: dict, environ=None) -> dict:
    """就地叠加环境变量覆盖并返回同一 dict。

    只在对应 env 变量「存在」时覆盖（空串也算存在——允许用 env 显式清空一个配置）；
    没设任何 env 变量时 cfg 一字不动。类型按 SCHEMA 默认值参照转换；解析失败的
    env 值整个跳过（保留 cfg 现值，不管它来自 config.json 还是 DEFAULTS）。
    """
    env = os.environ if environ is None else environ
    for env_name, key in ENV_MAP.items():
        legacy_name = env_name.replace("RXYY_MCP_", "CHIJIU_", 1)
        source_name = env_name if env_name in env else (legacy_name if legacy_name in env else "")
        if source_name:
            field = _BY_KEY[key]
            value = _coerce(env[source_name], field.default)
            if value is _INVALID:
                continue
            cfg[key] = value
    return cfg


def _mask(value: str) -> str:
    """凭证打码：留前 4 位给人对认，其余抹掉；短值整体抹。"""
    s = str(value)
    if not s:
        return ""
    return (s[:4] + "…" + "*" * 6) if len(s) > 4 else "*" * 6


def redact(cfg: dict) -> dict:
    """返回脱敏副本：敏感且非空的项打码，其余原样。不改传入的 cfg。

    给日志、错误横幅、配置导出、分享快照用——凭证绝不原文外泄。
    """
    out = dict(cfg)
    for key in SECRET_KEYS:
        if out.get(key):
            out[key] = _mask(out[key])
    return out


def example_config() -> dict:
    """开源用户的 config.example.json 内容：SCHEMA 覆盖的对外项，敏感项一律留空。"""
    return {f.key: ("" if f.secret else f.default) for f in SCHEMA}


def render_example_json() -> str:
    """带逐项注释的 config.example.json 文本（json 不支持注释，用并排的 _comment_<key>）。"""
    import json
    doc: dict[str, object] = {
        "_readme": ("rxyy MCP 配置模板。凭证项(secret)建议不写这里，改用对应环境变量注入："
                    "设了 env 就覆盖本文件。各项说明见 _comment_<键>。"),
    }
    for f in SCHEMA:
        tag = "【凭证·建议走 {}】".format(f.env) if f.secret else ""
        req = "【对外必填】" if f.required else ""
        doc["_comment_" + f.key] = "{}{}{}（env: {}）".format(req, tag, f.desc, f.env)
        doc[f.key] = "" if f.secret else f.default
    return json.dumps(doc, ensure_ascii=False, indent=2)


def missing_required(cfg: dict) -> list[ConfigField]:
    """列出「标了 required 但当前为空」的项——首次运行向导据此提示。"""
    return [f for f in SCHEMA if f.required and not str(cfg.get(f.key) or "").strip()]


def _wizard() -> int:
    """首次运行向导：把 example 写到 DATA_DIR/config.example.json 并打印缺配引导。

    独立入口，不接入 hub 启动；跑不跑都不影响现有运行。
    """
    import sys
    from pathlib import Path
    try:
        from datadir import DATA_DIR
        target_dir = Path(DATA_DIR)
    except Exception:
        target_dir = Path(__file__).resolve().parent
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    example = target_dir / "config.example.json"
    try:
        example.write_text(render_example_json(), encoding="utf-8")
        print("已生成配置模板：{}".format(example))
    except OSError as e:
        print("写配置模板失败：{}".format(e))
        return 1
    # 读现有 config.json（若有），报告缺哪些对外必填项
    cfg = {}
    try:
        import json
        cfg = json.loads((target_dir / "config.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    miss = missing_required(cfg)
    if miss:
        print("\n还需配置（对外提供服务必填）：")
        for f in miss:
            print("  · {}：{}（可设环境变量 {}）".format(f.key, f.desc, f.env))
    else:
        print("\n对外必填项已齐（或纯本机自用无需额外配置）。")
    print("\n凭证项建议用环境变量注入，不落 config.json：")
    for f in SCHEMA:
        if f.secret:
            print("  · {}  →  {}".format(f.env, f.key))
    return 0


if __name__ == "__main__":
    raise SystemExit(_wizard())
