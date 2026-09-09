# -*- coding: utf-8 -*-
"""把 extbus-ext/ 打成 VSIX 并（可选）装进 Cursor。不依赖 vsce / npm。

VSIX 就是一个 zip：`[Content_Types].xml` + `extension.vsixmanifest` + `extension/…`。
这里用标准库 zipfile 手工打，家机 / 公司机只要有 python 就能装，不用先装 node 工具链。

用法：
    py -3.11 rxyy_mcp\\extbus_build.py                 # 只打包 → 输出 vsix 路径
    py -3.11 rxyy_mcp\\extbus_build.py --install       # 打包 + cursor --install-extension
    py -3.11 rxyy_mcp\\extbus_build.py --install --cursor "E:\\cursor\\resources\\app\\bin\\cursor.cmd"

装完：**已经开着的窗口要 Reload Window 才会加载**（Cursor 只对新窗口自动生效）。
控制台里看不到某个窗口 = 那个窗口还没重载。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

APP_DIR = Path(__file__).resolve().parent
EXT_DIR = APP_DIR / "extbus-ext"
EXT_FILES = ("package.json", "extension.js", "README.md")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".md" ContentType="text/markdown"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Tags></Tags>
    <Categories>Other</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def read_manifest(ext_dir: Path = EXT_DIR) -> dict:
    return json.loads((ext_dir / "package.json").read_text(encoding="utf-8"))


def vsix_name(meta: dict) -> str:
    return "%s.%s-%s.vsix" % (meta["publisher"], meta["name"], meta["version"])


def build_vsix(out_dir: Path, ext_dir: Path = EXT_DIR) -> Path:
    """把 ext_dir 打成 VSIX，返回文件路径。重复打包覆盖同名旧包。"""
    meta = read_manifest(ext_dir)
    for f in EXT_FILES:
        if not (ext_dir / f).is_file():
            raise FileNotFoundError("扩展缺文件：%s" % (ext_dir / f))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / vsix_name(meta)
    manifest = MANIFEST.format(
        name=escape(meta["name"]), version=escape(meta["version"]),
        publisher=escape(meta["publisher"]), display=escape(meta.get("displayName") or meta["name"]),
        description=escape(meta.get("description") or ""),
        engine=escape((meta.get("engines") or {}).get("vscode") or "^1.80.0"))
    tmp = out.with_suffix(".vsix.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest)
        for f in EXT_FILES:
            z.write(ext_dir / f, "extension/" + f)
    os.replace(tmp, out)
    return out


def find_cursor_cli(explicit: str | None = None) -> str | None:
    """找 Cursor 的命令行入口（bin\\cursor.cmd）。

    顺序：显式参数 → PATH 里的 cursor → 正在跑的 Cursor.exe 旁边 → 常见安装位置。
    """
    if explicit:
        return explicit if Path(explicit).exists() else None
    hit = shutil.which("cursor") or shutil.which("cursor.cmd")
    if hit:
        return hit
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process Cursor -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
                capture_output=True, text=True, timeout=15)
            exe = (r.stdout or "").strip()
            if exe:
                cand = Path(exe).parent / "resources" / "app" / "bin" / "cursor.cmd"
                if cand.is_file():
                    return str(cand)
        except (OSError, subprocess.SubprocessError):
            pass
        local = os.environ.get("LOCALAPPDATA") or ""
        for cand in (Path(local) / "Programs" / "cursor" / "resources" / "app" / "bin" / "cursor.cmd",
                     Path("E:/cursor/resources/app/bin/cursor.cmd"),
                     Path("C:/Program Files/cursor/resources/app/bin/cursor.cmd")):
            if cand.is_file():
                return str(cand)
    return None


def install_vsix(vsix: Path, cursor_cli: str) -> tuple[int, str]:
    """cursor --install-extension；返回 (退出码, 输出)。已开着的窗口要重载才生效。"""
    cmd = [cursor_cli, "--install-extension", str(vsix), "--force"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=False)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包 / 安装 rxyy MCP 窗口总线扩展")
    ap.add_argument("--out", default=str(APP_DIR.parent / "dist_export" / "extbus"),
                    help="vsix 输出目录（默认 dist_export/extbus/）")
    ap.add_argument("--install", action="store_true", help="打包后立刻 cursor --install-extension")
    ap.add_argument("--cursor", default=None, help="cursor.cmd 的路径；不给就自动找")
    a = ap.parse_args(argv)
    vsix = build_vsix(Path(a.out))
    print("VSIX:", vsix, "(%d bytes)" % vsix.stat().st_size)
    if not a.install:
        return 0
    cli = find_cursor_cli(a.cursor)
    if not cli:
        print("找不到 cursor 命令行入口（bin\\cursor.cmd）；用 --cursor 指一下", file=sys.stderr)
        return 2
    code, out = install_vsix(vsix, cli)
    print("cursor --install-extension →", code)
    print(out)
    if code == 0:
        print("已装。已开着的 Cursor 窗口要 Reload Window（或新开窗口）才会加载扩展。")
    return code


if __name__ == "__main__":
    sys.exit(main())
