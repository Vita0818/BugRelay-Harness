"""Bug Relay 框架通用工具。

职责：
- 加载 config.json / state/match.json，原子化保存 state；
- 写操作日志 state/log.jsonl（最新记录在最上方）；
- 提供以本仓库根目录（harness_repo）为锚点的路径解析，避免受启动 cwd 影响。

本模块不含任何被评测的业务代码，也绝不创建 arena_repo。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# harness_repo 根目录：所有框架自身路径（state/backups/prompts/...）都以它为锚点
BASE_DIR = Path(__file__).resolve().parent.parent

# 规范【3】定义的默认配置（config.json 缺失时兜底，保证框架永不崩溃）
DEFAULT_CONFIG: Dict[str, Any] = {
    "arena_repo_path": "../arena_repo",
    "business_dir": "src",
    "history_tests_dir": "tests",
    "verifier_model": {
        "provider": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "model": "llama-3.1:8b",
        "api_key": "ollama",
        "timeout_seconds": 600,
    },
    "pytest_args": ["-q"],
    "state_file": "state/match.json",
    "hidden_tests_dir": "hidden_tests",
    "prompts_dir": "prompts",
    "backups_dir": "backups",
    "inbox_dir": "inbox",
}

# 运行时目录/文件都挂在 BASE_DIR 下，运行时按需创建（tmp/ 不预建）
TMP_DIR = BASE_DIR / "tmp"
UPLOADS_DIR = TMP_DIR / "uploads"

# 日志文件保护上限
_MAX_LOG_ENTRIES = 3000
_KEEP_LOG_ENTRIES = 1000

_LOCK = threading.RLock()


def default_state() -> Dict[str, Any]:
    """规范【4】定义的初始 state，外加少量运行时字段（phase/status/pending 等）。

    state/match.json 初始文件只含规范字段；框架启动时会自动合并这里的运行时字段。
    """
    return {
        "round": 1,
        "order": ["A", "B", "C", "D"],
        "survivors": ["A", "B", "C", "D"],
        "eliminated": [],
        "current_index": 0,
        "current_player": "A",
        "current_prompt_file": None,
        "arena_ready": False,
        "last_result": None,
        "scores": {},
        # ---- 运行时扩展字段（向后兼容，缺省自动补齐） ----
        "phase": "answering",        # answering=等待验收答题；proposing=等待校验出题
        "status": "running",         # running / finished
        "pending_answer": None,      # 已导入待验收的答题材料路径（.zip 或目录）
        "pending_proposal": None,    # 已导入待校验的出题材料 {"prompt": ..., "test": ...}
        "last_test_summary": None,   # 最近一次评测的两行计数（仅计数，不含测试内容）
        "last_action_msg": None,     # 最近一次操作的简短人类可读说明
    }


def load_config() -> Dict[str, Any]:
    """加载 config.json；文件不存在或损坏时返回默认配置副本（框架必须能启动）。"""
    cfg_path = BASE_DIR / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    try:
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        # 配置损坏不致命：用默认值继续
        pass
    return cfg


def resolve_path(rel: str | Path) -> Path:
    """把相对路径解析为基于 BASE_DIR 的绝对路径（绝对路径原样返回）。"""
    p = Path(rel)
    return p if p.is_absolute() else (BASE_DIR / p)


def arena_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """返回配置指向的 arena_repo 绝对路径（仅计算路径，绝不创建目录）。

    路径来源优先级（跨机器部署设计，Ubuntu 开箱即用）：
    1. 环境变量 BUGRELAY_ARENA_REPO（可绝对/相对/`~` 开头，无需改文件即可换机器）；
    2. config.json 的 arena_repo_path（相对路径基于 harness_repo 根解析，支持 `~`）。
    """
    cfg = cfg or load_config()
    raw = os.environ.get("BUGRELAY_ARENA_REPO") or str(cfg.get("arena_repo_path", "../arena_repo"))
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


def state_file_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg or load_config()
    return resolve_path(cfg.get("state_file", "state/match.json"))


def inbox_dir_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """材料投递目录 inbox/（Agent 交付物落点，人类按按钮时框架自动拾取）。"""
    cfg = cfg or load_config()
    return resolve_path(cfg.get("inbox_dir", "inbox"))


def load_state() -> Dict[str, Any]:
    """读取 state/match.json；不存在时返回初始 state（合并运行时扩展字段）。"""
    with _LOCK:
        sp = state_file_path()
        state = default_state()
        try:
            if sp.exists():
                with open(sp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 合并：磁盘上保存的值优先，缺的运行时字段用默认补齐
                    merged = default_state()
                    merged.update(data)
                    state = merged
        except Exception:
            # state 损坏时重置为初始（记录日志的尝试也可能失败，直接吞掉）
            state = default_state()
        return state


def save_state(state: Dict[str, Any]) -> None:
    """原子化保存 state（先写临时文件再 rename，避免写一半损坏）。"""
    with _LOCK:
        sp = state_file_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_suffix(sp.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sp)


def set_player_models(updates: Dict[str, str]) -> Dict[str, Any]:
    """批量更新选手"实际模型"名称（模型迭代时用；Web/CLI 共用）。

    - updates：{三字码: 新实际模型名}，如 {"FBL": "Claude Fable 5.5"}；
    - 只改 state.players.<三字码>.model；三字码、总称（name）、积分/淘汰/日志等
      历史记录一概不动（这正是用三字码做主键的意义）；
    - 校验：三字码必须已存在于 players，模型名必须非空；任一非法则整体抛
      ValueError（不做半截更新）；成功后写 set-model 日志，返回更新后的 state。
    """
    state = load_state()
    players = state.get("players") or {}
    problems: List[str] = []
    for code, model in (updates or {}).items():
        if not isinstance(model, str) or not model.strip():
            problems.append("%s：模型名不能为空" % code)
        elif code not in players:
            problems.append("未知选手三字码：%s" % code)
    if problems:
        raise ValueError("；".join(problems))
    changed: List[str] = []
    for code, model in updates.items():
        old = (players.get(code) or {}).get("model") or "（空）"
        new = model.strip()
        players[code]["model"] = new
        changed.append("%s %s → %s" % (code, old, new))
    state["players"] = players
    save_state(state)
    log_event("set-model", "更新选手实际模型：" + "；".join(changed))
    return state


def log_file_path() -> Path:
    return state_file_path().parent / "log.jsonl"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(action: str, detail: str = "", result: str = "INFO",
              player: Optional[str] = None, round_: Optional[int] = None) -> None:
    """追加一条操作日志到 state/log.jsonl。

    文件内"最新在最上"：读出全部旧记录，把新事件插到头部后整体重写。
    日志只记录框架操作文字与统计计数，绝不写入测试内容/断言/diff。
    """
    entry = {
        "ts": now_iso(),
        "action": action,
        "result": result,          # INFO / PASS / FAIL / WARN
        "player": player,
        "round": round_,
        "detail": detail,
    }
    with _LOCK:
        lp = log_file_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        old: List[Dict[str, Any]] = []
        try:
            if lp.exists():
                with open(lp, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            old.append(json.loads(line))
                        except Exception:
                            continue
        except Exception:
            old = []
        # 新事件插到最前 -> 文件中最新在上
        entries = [entry] + old
        if len(entries) > _MAX_LOG_ENTRIES:
            entries = entries[:_KEEP_LOG_ENTRIES]
        with open(lp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_log(limit: int = 100) -> List[Dict[str, Any]]:
    """读取日志（文件本身最新在上），返回前 limit 条。"""
    items: List[Dict[str, Any]] = []
    lp = log_file_path()
    try:
        if not lp.exists():
            return items
        with open(lp, "r", encoding="utf-8") as f:
            for line in f:
                if len(items) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return items


def ensure_dirs() -> None:
    """确保框架自身的目录与占位文件存在（不创建 arena_repo，不创建业务目录）。"""
    cfg = load_config()
    for key in ("hidden_tests_dir", "prompts_dir", "backups_dir", "inbox_dir"):
        d = resolve_path(cfg.get(key, key))
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
    # state 目录 + 初始 state 文件 + 空日志文件
    sp = state_file_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if not sp.exists():
        save_state(default_state())
    lp = log_file_path()
    if not lp.exists():
        lp.write_text("", encoding="utf-8")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
