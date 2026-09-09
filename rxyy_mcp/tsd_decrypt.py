# -*- coding: utf-8 -*-
"""TEC OCULAR / IPGuard (TsdEncryptMF) 透明加密文件的原地解密。

原理（源自 D:\\Desktop\\jiami\\decrypt_all.py 的逆向成果）：
  驱动对受监控扩展名文件透明加密；唯一可靠的原地解密 = 调驱动自带
  SDEncryptionAPI64.dll::SDDecryptFileW(path, "x")，但需先内存 patch
  SDValidationHelper(RVA 0x17652: mov eax,0x15022 → mov eax,0)。
  解密持久、幂等；对未加密文件是安全 no-op（rc!=0，内容不变）。

设计为「本机有 DLL 才生效，否则静默降级」：非公司机器 / DLL 缺失 /
IPGuard 版本变动导致 patch 字节不匹配时，全部返回失败而不抛异常，
调用方（rxyy MCP 落盘、codebrain CLI）不受影响。
"""
import ctypes
import os
import subprocess
import threading
from ctypes import wintypes

TSD_MAGIC = "%TSD-Header-###%"
_SD_RVA = 0x17652
_FAIL = bytes([0xB8, 0x22, 0x50, 0x01, 0x00])   # mov eax, 0x15022
_PATCH = bytes([0xB8, 0x00, 0x00, 0x00, 0x00])  # mov eax, 0
_CREATE_NO_WINDOW = 0x08000000

# 受驱动监控的扩展名（逆向所得 + 常见补充）；其余扩展名不会被加密，跳过。
MONITORED_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".wps", ".et", ".dps",
    ".pdf", ".txt", ".rtf", ".md", ".csv", ".log",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".dwg", ".dxf", ".dwt", ".cad", ".prt", ".asm", ".iges", ".step", ".stp", ".igs",
    ".psd", ".ai", ".cdr",
    ".cpp", ".c", ".h", ".hpp", ".cc", ".cs", ".java", ".py", ".go", ".rs",
    ".js", ".ts", ".jsx", ".tsx", ".vue", ".php", ".rb", ".lua", ".sql", ".sh",
    ".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".properties",
    ".html", ".htm", ".css", ".scss", ".less",
}

_dll = None
_dll_ready = False
_lock = threading.Lock()


def _load_dll():
    """惰性加载并 patch DLL，只做一次；不可用时返回 None（不抛）。"""
    global _dll, _dll_ready
    with _lock:
        if _dll_ready:
            return _dll
        _dll_ready = True
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            dll = ctypes.WinDLL("SDEncryptionAPI64.dll")
            base = kernel32.GetModuleHandleW("SDEncryptionAPI64.dll")
            addr = base + _SD_RVA
            cur = bytes((ctypes.c_ubyte * 5).from_address(addr))
            if cur == _FAIL:
                VP = kernel32.VirtualProtect
                VP.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD)]
                VP.restype = wintypes.BOOL
                old = wintypes.DWORD(0)
                VP(addr, 5, 0x40, ctypes.byref(old))
                buf = (ctypes.c_ubyte * 5).from_address(addr)
                for i, b in enumerate(_PATCH):
                    buf[i] = b
                VP(addr, 5, old.value, ctypes.byref(old))
                cur = bytes((ctypes.c_ubyte * 5).from_address(addr))
            if cur != _PATCH:
                return None  # 已被别处 patch 成别的样子 / RVA 变动，放弃
            dll.SDDecryptFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            dll.SDDecryptFileW.restype = ctypes.c_uint
            _dll = dll
        except Exception:
            _dll = None
        return _dll


def available():
    """本机解密能力是否可用（DLL 能加载并 patch 成功）。"""
    return _load_dll() is not None


def is_encrypted(path):
    """检测文件当前是否为密文（certutil 是非授信进程，能看到原始 magic）。"""
    try:
        r = subprocess.run(["certutil", "-dump", path], capture_output=True,
                           text=True, encoding="gbk", errors="ignore", timeout=25,
                           creationflags=_CREATE_NO_WINDOW)
        return TSD_MAGIC in r.stdout
    except Exception:
        return False


def decrypt_file(path):
    """原地解密单个文件；幂等（对未加密文件是安全 no-op）。
    返回 True=已确保明文；False=不可用/失败。绝不抛异常。"""
    dll = _load_dll()
    if dll is None:
        return False
    try:
        if os.path.splitext(path)[1].lower() not in MONITORED_EXTS:
            return True  # 非受控扩展名，本就不会被加密
        dll.SDDecryptFileW(str(path), "x")
        return True
    except Exception:
        return False


def decrypt_dir(root, exts=None):
    """递归原地解密目录下所有受监控文件。返回处理的文件数。"""
    if _load_dll() is None:
        return 0
    exts = exts or MONITORED_EXTS
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".cursor", "site-packages"}
    n = 0
    for dirpath, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for fn in fnames:
            if os.path.splitext(fn)[1].lower() in exts:
                if decrypt_file(os.path.join(dirpath, fn)):
                    n += 1
    return n
