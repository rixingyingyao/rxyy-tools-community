"""Bounded uploads for the local Codex owner; no client-supplied local paths."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

MAX_FILES = 10
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def delivery_directory(root, thread_id, delivery_id):
    thread_id = str(uuid.UUID(str(thread_id)))
    delivery_id = str(uuid.UUID(str(delivery_id)))
    root = Path(root).resolve()
    target = root / thread_id / delivery_id
    if not target.resolve().is_relative_to(root):
        raise ValueError("附件目录无效")
    return target


def _image_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("图片需为 PNG、JPEG、GIF 或 WebP")


def prepare(root, thread_id, delivery_id, images=None, files=None):
    """Validate the whole upload before writing; reuse identical bytes on retries."""
    images, files = images or [], files or []
    if not isinstance(images, list) or not isinstance(files, list):
        raise ValueError("附件列表格式无效")
    if len(images) + len(files) > MAX_FILES:
        raise ValueError("每条消息最多 10 个附件")
    directory = delivery_directory(root, thread_id, delivery_id)
    checked, total = [], 0
    for kind, items in (("image", images), ("file", files)):
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("data"), str):
                raise ValueError("附件缺少上传内容；不能引用客户端本地路径")
            data = item["data"]
            if data.startswith("data:"):
                header, sep, data = data.partition(",")
                if not sep or not header.endswith(";base64"):
                    raise ValueError("附件需使用 base64 上传")
            if len(data) > ((MAX_FILE_BYTES + 2) // 3) * 4:
                raise ValueError("单个附件不能超过 16 MB")
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("附件编码无效") from exc
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("单个附件不能超过 16 MB")
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("每条消息的附件合计不能超过 32 MB")
            name = str(item.get("name") or item.get("filename") or
                       ("图片" if kind == "image" else "附件.bin"))[:160]
            # Names remain display labels, never directory or stream components.
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .") or "附件"
            media, extension = _image_type(raw) if kind == "image" else ("", "")
            if extension and not name.lower().endswith(extension):
                name += extension
            digest = hashlib.sha256(raw).hexdigest()
            stored = f"{len(checked) + 1:02d}-{digest[:16]}-{name}"
            checked.append((raw, {"kind": kind, "label": name,
                                  "path": str(directory / stored),
                                  "fsPath": str(directory / stored),
                                  "mimeType": media, "size": len(raw), "sha256": digest}))
    if checked:
        directory.mkdir(parents=True, exist_ok=True)
    for raw, attachment in checked:
        path = Path(attachment["path"])
        if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
            raise ValueError("附件路径无效")
        try:
            with path.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != attachment["sha256"]:
                raise ValueError("附件内容校验失败，请重新选择文件")
    return [attachment for _, attachment in checked]


def reserve(root, thread_id, delivery_id):
    """Persist before IPC. A retry cannot create a second delivery after a restart."""
    directory = delivery_directory(root, thread_id, delivery_id)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "receipt.json"
    try:
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump({"state": "submitting"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return False
    return True


def settle(root, thread_id, delivery_id, result):
    directory = delivery_directory(root, thread_id, delivery_id)
    receipt = directory / "receipt.json"
    temporary = directory / ("receipt.tmp." + uuid.uuid4().hex)
    data = {"state": result.get("delivery") if result.get("ok") else
            ("unknown" if result.get("delivery_unknown") else "rejected"),
            "turn_id": result.get("turn_id")}
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(receipt)
    finally:
        temporary.unlink(missing_ok=True)
