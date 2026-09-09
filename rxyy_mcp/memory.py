# -*- coding: utf-8 -*-
"""ji 记忆管理：格式与寸止完全兼容（.cunzhi-memory，"- " 行），已有记忆可直接复用"""
import uuid
from pathlib import Path

MEMORY_DIR_NAME = ".cunzhi-memory"

CATEGORY_FILES = {
    "rule": "rules.md",
    "preference": "preferences.md",
    "pattern": "patterns.md",
    "context": "context.md",
}

CATEGORY_TITLES = {
    "rule": "开发规范和规则",
    "preference": "用户偏好设置",
    "pattern": "常用模式和最佳实践",
    "context": "项目上下文信息",
}

CATEGORY_LABELS = [
    ("rule", "规范"),
    ("preference", "偏好"),
    ("pattern", "模式"),
    ("context", "背景"),
]


def _clean_path(raw: str) -> Path:
    s = (raw or "").strip().strip('"').strip("'")
    if s.startswith("file:///"):
        s = s[len("file:///"):]
    return Path(s).expanduser()


def find_git_root(start: Path):
    cur = start.resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


class MemoryManager:
    def __init__(self, project_path: str):
        p = _clean_path(project_path)
        if not p.exists():
            raise ValueError(f"项目路径不存在: {project_path}")
        if not p.is_dir():
            raise ValueError(f"项目路径不是目录: {project_path}")
        root = find_git_root(p)
        if root is None:
            raise ValueError(
                "错误：提供的项目路径不在 git 仓库中。\n"
                f"路径: {p}\n"
                "请确保在 git 根目录（包含 .git 文件夹的目录）中调用此功能。"
            )
        self.project_root = root
        self.dir = root / MEMORY_DIR_NAME
        self.dir.mkdir(exist_ok=True)
        for cat, fname in CATEGORY_FILES.items():
            f = self.dir / fname
            if not f.exists():
                f.write_text(f"# {CATEGORY_TITLES[cat]}\n\n", encoding="utf-8")

    def add(self, content: str, category: str) -> str:
        cat = category if category in CATEGORY_FILES else "context"
        f = self.dir / CATEGORY_FILES[cat]
        text = f.read_text(encoding="utf-8") if f.exists() else f"# {CATEGORY_TITLES[cat]}\n\n"
        # 去重：同一条记忆已存在就不重复追加（归一化空白后比较）
        norm = " ".join(content.strip().split())
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- ") and " ".join(line[2:].split()) == norm:
                return "duplicate"
        if not text.endswith("\n"):
            text += "\n"
        text += f"- {content.strip()}\n"
        f.write_text(text, encoding="utf-8")
        return str(uuid.uuid4())

    def entries(self, category: str):
        f = self.dir / CATEGORY_FILES[category]
        if not f.exists():
            return []
        items = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("- ") and len(line) > 2:
                content = line[2:].strip()
                if content:
                    items.append(content)
        return items

    def recall(self) -> str:
        parts = []
        for cat, label in CATEGORY_LABELS:
            items = [" ".join(e.split()) for e in self.entries(cat)]
            if items:
                parts.append(f"**{label}**: {'; '.join(items)}")
        if not parts:
            return "📭 暂无项目记忆"
        return "📚 项目记忆总览: " + " | ".join(parts)
