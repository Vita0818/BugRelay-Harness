"""arena_repo 仓库操作（只做机械操作，不含业务逻辑）。

职责：
- git 命令封装（rev-parse / checkout / clean）与历史测试内容哈希清单；
- arena_repo 的全量复制（供 RED/GREEN 与手动自证目录）；
- 上传包（zip/目录/单文件）的安全解压与"只进 business_dir"的应用；
- 目录清空与从备份还原的底层原语。

安全边界：
- 所有函数只对"已存在"的 arena_repo 操作，绝不创建 arena_repo；
- 路径安全校验：arena 指向 harness 自身/祖先/子目录时一律视为"未就绪"，防止误删；
- zip 解压带 Zip-Slip 防护；上传应用严格排除顶层历史测试目录。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .utils import BASE_DIR, UPLOADS_DIR, arena_path, load_config, load_state, save_state

# 复制/遍历时统一忽略的噪声目录与文件
IGNORE_NAMES = ("__pycache__", ".pytest_cache", ".git", ".DS_Store")
IGNORE_SUFFIXES = (".pyc", ".pyo")

# 文件树接口额外过滤的目录（规范【5】：文件树必须过滤 hidden_tests/）
TREE_IGNORE_NAMES = set(IGNORE_NAMES) | {"hidden_tests", "tmp", ".venv", "venv", "node_modules", ".idea", ".vscode"}
TRANSIENT_HIDDEN_PREFIXES = ("test_hidden_", "test_proposal_red_", "test_proposal_green_")


def _safe_arena(cfg: Optional[Dict] = None) -> Tuple[Optional[Path], Optional[str]]:
    """校验 arena_repo 路径安全性。

    返回 (arena_path, None) 表示安全；返回 (None, 原因) 表示不可用。
    防止配置事故（arena 指向 harness 自身 / 祖先 / 子目录）导致误删或泄漏。
    """
    cfg = cfg or load_config()
    p = arena_path(cfg)
    if not p.exists():
        return None, "arena_repo 不存在（%s）" % p
    rp = p.resolve()
    bd = BASE_DIR.resolve()
    if rp == bd:
        return None, "arena_repo_path 非法：指向 harness_repo 自身"
    if bd in rp.parents:
        return None, "arena_repo_path 非法：arena 位于 harness_repo 内部"
    if rp in bd.parents:
        return None, "arena_repo_path 非法：arena 是 harness_repo 的祖先目录"
    return rp, None


def is_arena_ready() -> bool:
    """arena_repo 是否就绪：路径存在、安全、且是一个 git 仓库。"""
    p, err = _safe_arena()
    if p is None:
        return False
    if (p / ".git").exists():
        return True
    # 兼容 .git 为文件（worktree/submodule）等情况
    r = git(p, "rev-parse", "--git-dir")
    return r.returncode == 0


def refresh_arena_ready() -> bool:
    """刷新 state 中的 arena_ready 字段（供 Web 启动 / CLI 每次执行前调用）。

    arena 就绪时顺带做**项目指令/规范注入自愈**：AGENTS.md 托管区块或
    TESTING_GUIDELINES.md 缺失/过时就重写（core/rules.inject_rules 幂等），并把 rules_injected
    写入 state。这样人类把 arena_repo 接入框架的那一刻，规范就已在仓库里。
    """
    ready = is_arena_ready()
    state = load_state()
    new_rules = False
    if ready:
        from . import rules  # 延迟导入：rules 依赖本模块的 _safe_arena，防循环
        rules.inject_rules()
        new_rules = rules.rules_injected()
    if state.get("arena_ready") != ready or state.get("rules_injected", False) != new_rules:
        state["arena_ready"] = ready
        state["rules_injected"] = new_rules
        save_state(state)
    return ready


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在指定仓库执行 git 命令（check=False，由调用方判断 returncode）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def current_commit(repo: Optional[Path] = None) -> Optional[str]:
    """返回仓库当前 commit（git rev-parse HEAD），失败返回 None。"""
    repo = repo or arena_path()
    try:
        r = git(repo, "rev-parse", "HEAD")
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def tests_status_map(ignore_names: Optional[set[str]] = None) -> Dict[str, str]:
    """返回历史测试目录的内容级清单。

    清单覆盖目录、普通文件内容哈希和符号链接目标，因此已跟踪、未跟踪或原本已修改的
    测试都能被准确比较；ignore_names 用于 pytest 期间排除框架临时注入的新测试文件。
    """
    cfg = load_config()
    p, err = _safe_arena(cfg)
    tdir = cfg.get("history_tests_dir", "tests")
    if p is None:
        return {}
    root = p / tdir
    if not root.exists():
        return {}
    ignored = set(ignore_names or set())
    mapping: Dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        rel = path.relative_to(p)
        if path.name in ignored:
            continue
        if any(part in ("__pycache__", ".pytest_cache") for part in rel.parts):
            continue
        if path.suffix in IGNORE_SUFFIXES or path.name == ".DS_Store":
            continue
        key = rel.as_posix()
        if path.is_symlink():
            mapping[key] = "link:" + os.readlink(path)
        elif path.is_dir():
            mapping[key] = "dir"
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            mapping[key] = "file:" + digest.hexdigest()
    return mapping


def _ignore_filter(name: str) -> bool:
    return name in IGNORE_NAMES or name.endswith(IGNORE_SUFFIXES)


def copy_arena_to(dst: Path, for_verify: bool = True) -> None:
    """把 arena_repo 全量复制到 dst（RED/GREEN 或手动自证工作区）。

    for_verify=True 时额外排除 .git / tmp / hidden_tests（临时目录跑 pytest 不需要 git；
    hidden_tests 本就位于 harness 内，此处为防御性排除）；
    for_verify=False（备份场景）保留 .git，仅排除噪声目录。
    """
    p, err = _safe_arena()
    if p is None:
        raise RuntimeError("arena_repo 未就绪: %s" % (err or ""))
    if for_verify:
        patterns = tuple(set(IGNORE_NAMES + (".git", "tmp", "hidden_tests")))
    else:
        # 手动自证工作区保留 .git，让 OpenCode/真人获得与正式仓库一致的上下文；
        # 后续锁定清单会忽略 .git 的运行时变化，但仍保护 tests/ 与根级配置。
        patterns = tuple(name for name in IGNORE_NAMES if name != ".git")
    shutil.copytree(p, dst, ignore=shutil.ignore_patterns(*patterns), dirs_exist_ok=True)


def clean_dir_contents(target: Path) -> None:
    """清空目录内所有条目（保留目录本身）。用于还原前的腾空。"""
    for child in target.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def extract_zip_safe(zip_path: Path, dest: Path) -> Tuple[bool, str]:
    """安全解压 zip 到 dest（Zip-Slip 防护：所有目标必须落在 dest 内）。"""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        dest_r = str(dest.resolve())
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                target = (dest / info.filename).resolve()
                if not str(target).startswith(dest_r + os.sep):
                    return False, "zip 内含非法路径: %s" % info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return True, ""
    except zipfile.BadZipFile as e:
        return False, "zip 文件损坏: %s" % e
    except Exception as e:
        return False, str(e)


def sanitize_rel_name(name: str) -> str:
    """清洗上传文件名（可能带 webkitdirectory 相对路径）：去非法字符与穿越。"""
    name = (name or "").replace("\\", "/").strip()
    parts = [seg for seg in name.split("/") if seg not in ("", ".", "..")]
    return "/".join(parts)


def apply_upload_to_business(upload_path: str | Path) -> Dict:
    """把上传的"业务代码改动"应用到 arena_repo/business_dir。

    支持三种形态：
      - .zip 文件：安全解压后应用；
      - 目录：直接应用其内容；
      - 单个文件：作为 business_dir 下一个文件应用。
    规则（规范【5】）：
      - 只写入 business_dir（默认 src/），因此天然够不到 arena 顶层 tests/；
      - 上传内容顶层的 tests/ 目录整目录跳过（防篡改企图，记录 warning）；
      - zip 内唯一顶层目录恰为 business_dir 名（如 src/）时自动剥层；
      - tests/ 内容哈希的前后对比由 judge.verify_answer 负责。
    返回 {"ok", "applied", "skipped", "error", "warning"}。
    """
    cfg = load_config()
    p, err = _safe_arena(cfg)
    if p is None:
        return {"ok": False, "applied": 0, "skipped": 0, "error": "arena_repo 未就绪: %s" % (err or ""), "warning": ""}

    biz_name = cfg.get("business_dir", "src")
    tdir_name = cfg.get("history_tests_dir", "tests")
    up = Path(upload_path)

    staging: Optional[Path] = None
    if up.is_file() and up.suffix.lower() == ".zip":
        staging = BASE_DIR / "tmp" / ("extract_%s" % os.urandom(4).hex())
        ok, zerr = extract_zip_safe(up, staging)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "applied": 0, "skipped": 0, "error": "解压失败: %s" % zerr, "warning": ""}
        root = staging
    elif up.is_dir():
        root = up
    elif up.is_file():
        staging = BASE_DIR / "tmp" / ("extract_%s" % os.urandom(4).hex())
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copy2(up, staging / up.name)
        root = staging
    else:
        return {"ok": False, "applied": 0, "skipped": 0, "error": "上传路径不存在: %s" % up, "warning": ""}

    # 剥层：唯一顶层目录且名为 business_dir（常见 "src/xxx" 打包结构）
    try:
        entries = [e for e in root.iterdir() if not _ignore_filter(e.name)]
        if len(entries) == 1 and entries[0].is_dir() and entries[0].name == biz_name:
            root = entries[0]
    except Exception:
        pass

    biz_dir = p / biz_name
    applied, skipped = 0, 0
    warning = ""
    try:
        biz_dir.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _ignore_filter(d)]
            rel_dir = Path(dirpath).relative_to(root)
            # 顶层历史测试目录整目录跳过：严禁触碰 arena 的 tests/
            if rel_dir.parts and rel_dir.parts[0] == tdir_name:
                skipped += len(filenames)
                warning = "上传内容包含顶层 %s/ 目录，已整体跳过（%d 个文件）。严禁修改历史测试。" % (tdir_name, skipped)
                dirnames[:] = []
                continue
            for fn in filenames:
                if _ignore_filter(fn):
                    continue
                rel = (rel_dir / fn) if str(rel_dir) != "." else Path(fn)
                src_file = Path(dirpath) / fn
                dest = biz_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest)
                applied += 1
    except Exception as e:
        return {"ok": False, "applied": applied, "skipped": skipped, "error": "应用文件失败: %s" % e, "warning": warning}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    if applied == 0:
        return {"ok": False, "applied": 0, "skipped": skipped,
                "error": "上传包中没有可应用的业务文件", "warning": warning}
    return {"ok": True, "applied": applied, "skipped": skipped, "error": "", "warning": warning}


def build_tree(max_entries: int = 2000) -> Optional[Dict]:
    """构建 arena_repo 文件树（供 Web 左栏展示）。

    过滤规则（规范【5】）：必须过滤 hidden_tests/ 目录，同时忽略
    .git/__pycache__/.pytest_cache/venv 等噪声。超限截断。
    arena 未就绪时返回 None。
    """
    p, err = _safe_arena()
    if p is None:
        return None

    counter = {"n": 0}

    def _walk(d: Path) -> List[Dict]:
        nodes: List[Dict] = []
        try:
            children = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except Exception:
            return nodes
        for c in children:
            if counter["n"] >= max_entries:
                break
            if c.name in TREE_IGNORE_NAMES or c.name.endswith(IGNORE_SUFFIXES):
                continue
            if c.is_file() and c.name.startswith(TRANSIENT_HIDDEN_PREFIXES):
                continue
            counter["n"] += 1
            if c.is_dir():
                nodes.append({"name": c.name, "path": str(c.relative_to(p)), "type": "dir",
                              "children": _walk(c)})
            else:
                nodes.append({"name": c.name, "path": str(c.relative_to(p)), "type": "file",
                              "children": []})
        return nodes

    tree = {"name": p.name, "path": "", "type": "dir", "children": _walk(p)}
    if counter["n"] >= max_entries:
        tree["truncated"] = True
    return tree


def read_arena_file(rel: str, max_bytes: int = 512 * 1024) -> Dict:
    """安全读取 arena_repo 内的文本文件（供 Web 文件预览）。

    安全校验：拒绝路径穿越（../、绝对路径）、拒绝任何以 hidden_tests 开头的路径；
    二进制（含 NUL 字节）与大文件不返回内容。
    """
    p, err = _safe_arena()
    if p is None:
        return {"ok": False, "error": "arena_repo 未就绪: %s" % (err or "")}
    rel = sanitize_rel_name(rel)
    if not rel:
        return {"ok": False, "error": "非法路径"}
    if rel.split("/")[0] == "hidden_tests":
        return {"ok": False, "error": "禁止访问 hidden_tests"}
    if Path(rel).name.startswith(TRANSIENT_HIDDEN_PREFIXES):
        return {"ok": False, "error": "禁止访问评测中的临时隐藏测试"}
    target = (p / rel).resolve()
    if not str(target).startswith(str(p) + os.sep):
        return {"ok": False, "error": "路径越界"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "文件不存在"}
    size = target.stat().st_size
    if size > max_bytes:
        return {"ok": True, "path": rel, "content": None, "lines": 0,
                "message": "文件过大（%d KB），不提供预览" % (size // 1024)}
    data = target.read_bytes()
    if b"\x00" in data[:4096]:
        return {"ok": True, "path": rel, "content": None, "lines": 0, "message": "二进制文件，不提供预览"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gbk")
        except Exception:
            return {"ok": True, "path": rel, "content": None, "lines": 0, "message": "无法解码的文件"}
    return {"ok": True, "path": rel, "content": text, "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}
