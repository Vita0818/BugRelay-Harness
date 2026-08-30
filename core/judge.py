"""Bug Relay 裁判核心（规范【6】）。

本模块是唯一会修改 arena_repo 的地方，且只在人类触发（Web 按钮 / CLI 命令）的瞬间工作：
导入文件 -> 跑 pytest -> 记录结果 -> 备份/回滚。框架不启动、不观测任何选手 Agent。

主要函数：
- run_pytest(repo_path, extra_test_file=None, hidden_name=None):
    在指定仓库运行 pytest（config.pytest_args）。extra_test_file 给定时先拷入
    目标 tests/ 再跑（历史+隐藏）。返回 dict（含 exit_code/passed/total/log_text，
    以及 history/hidden 的分别计数）。
- backup_arena(tag): 把 arena 当前完整状态复制到 backups/，登记 index.json，返回备份 id。
- restore_arena(): 从最近一次备份还原（无备份时用 git checkout+clean 兜底，保留 tests/）。
- apply_business_files(upload_path): 见 repo_ops.apply_upload_to_business（严格排除 tests/）。
- verify_answer(): 答题验收（不调用任何模型）。
- verify_proposal(): 出题验题（唯一调用 verifier_model 的地方，且只调用一次）。

模型调用约定（规范【8】）：验题模型严格按 "=== FILE: 相对路径 ===" 文件块输出，
解析失败（没有任何 FILE 块）直接判 FAIL，不重试。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import repo_ops
from .utils import (
    BASE_DIR, UPLOADS_DIR, arena_path, inbox_dir_path, load_config, load_state,
    log_event, now_iso, resolve_path, save_state,
)

# 全局裁判锁：防止 Web 双击 / CLI 并发导致两次评测同时改 arena
VERIFY_LOCK = threading.Lock()

# pytest 单次运行的保护超时（防止测试死循环卡死框架）
PYTEST_TIMEOUT_SECONDS = 1800

# 验题模型系统提示（规范【8】：只要文件块，不要解释）
VERIFIER_SYSTEM_PROMPT = """你是一名资深 Python 程序员，正在参加编程接力赛的验题环节。
用户消息是一份需求文档（next_prompt.md）。你的任务：仅凭这份需求文档，在已有 Python 项目中实现所需功能（可以新增或修改任意业务代码文件）。

你必须严格按以下格式输出，每个文件一个块，除此之外不要输出任何解释、前言、结语或 Markdown 代码围栏：
=== FILE: 相对路径 ===
（该文件的完整内容）
=== FILE: 另一个相对路径 ===
（该文件的完整内容）

规则：
1. 只输出需要新增或修改的文件；路径是相对项目根目录的路径（业务代码通常位于 src/ 下）。
2. 禁止输出或修改 tests/ 目录下的任何文件。
3. 不要输出任何其他文字。"""

# 文件块头部："=== FILE: <相对路径> ==="
_FILE_HEADER_RE = re.compile(r"===\s*FILE:\s*(.+?)\s*===")


# ---------------------------------------------------------------------------
# 基础：state 帮助函数
# ---------------------------------------------------------------------------

def _set_msg(state: Dict, msg: str) -> None:
    state["last_action_msg"] = msg


def _eliminate(state: Dict, player: str) -> None:
    """淘汰：移出 survivors、进入 eliminated。此前成功提交的代码不受影响。"""
    if player in state.get("survivors", []):
        state["survivors"] = [p for p in state["survivors"] if p != player]
    if player not in state.get("eliminated", []):
        state["eliminated"].append(player)


def _advance(state: Dict, eliminate_current: bool) -> bool:
    """推进到下一位存活选手。

    - eliminate_current=True：当前选手已移出 survivors，从下一位开始找存活者；
    - eliminate_current=False（交棒）：当前选手保留，找"下一位"存活者（跳过自己）；
    - 找不到（人没了 / 只剩自己）时置 status=finished，返回 False。
    """
    order = state.get("order", [])
    n = len(order)
    if n == 0:
        state["status"] = "finished"
        return False
    if eliminate_current:
        _eliminate(state, state.get("current_player", ""))
    limit = n if eliminate_current else n - 1
    for k in range(1, max(limit, 0) + 1):
        idx = (state.get("current_index", 0) + k) % n
        cand = order[idx]
        if cand in state.get("survivors", []):
            state["current_index"] = idx
            state["current_player"] = cand
            return True
    state["status"] = "finished"
    state["current_player"] = None
    return False


def _summary_counts(res: Dict) -> Dict:
    """把 run_pytest 结果压成前端可展示的两行计数（不含任何测试内容）。"""
    def pack(part: Dict) -> Dict:
        return {"passed": part.get("passed", 0), "total": part.get("total", 0),
                "ok": bool(part.get("total", 0) > 0 and part.get("passed", 0) >= part.get("total", 0))}
    return {"history": pack(res.get("history", {})), "hidden": pack(res.get("hidden", {})),
            "overall": "PASS" if res.get("ok") else "FAIL"}


# ---------------------------------------------------------------------------
# pytest 运行
# ---------------------------------------------------------------------------

def _parse_junit(xml_path: str, hidden_name: Optional[str]) -> Optional[Dict]:
    """解析 pytest junit xml，按文件归出 history / hidden 两份计数。

    使用 junit_family=legacy（testcase 带 file 属性）。解析失败返回 None（走 fallback）。
    计数口径：passed = 通过+跳过（与 pytest 退出码 0 语义对齐），total = 全部用例。
    """
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return None
    hist = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
    hid = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
    for tc in root.iter("testcase"):
        f = (tc.get("file") or "").replace("\\", "/")
        is_hidden = bool(hidden_name) and (os.path.basename(f) == hidden_name or f.endswith("/" + (hidden_name or "\x00")))
        bucket = hid if is_hidden else hist
        bucket["total"] += 1
        if tc.find("failure") is not None:
            bucket["failed"] += 1
        elif tc.find("error") is not None:
            bucket["errors"] += 1
        else:  # passed / skipped 均计为通过（skipped 不视为失败）
            bucket["passed"] += 1
    for b in (hist, hid):
        b["passed"] = b["total"] - b["failed"] - b["errors"]
    return {"history": hist, "hidden": hid}


def _parse_summary_fallback(stdout: str, hidden_name: Optional[str]) -> Dict:
    """从 -q 输出末行解析计数（junit xml 不可用时的兜底，无法分离 history/hidden）。"""
    passed = failed = errors = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error|errors)", stdout):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    total = passed + failed + errors
    hist = {"passed": 0 if hidden_name else passed, "failed": 0, "errors": 0, "total": 0 if hidden_name else total}
    hid = {"passed": passed, "failed": failed, "errors": errors, "total": total} if hidden_name \
        else {"passed": 0, "failed": 0, "errors": 0, "total": 0}
    return {"history": hist, "hidden": hid}


def run_pytest(repo_path: str | Path, extra_test_file: Optional[str | Path] = None,
               hidden_name: Optional[str] = None) -> Dict:
    """在指定仓库运行 pytest（规范【6】）。

    - 命令：`<当前解释器> -m pytest <config.pytest_args> --junitxml=<系统临时文件> -o junit_family=legacy --tb=short -p no:cacheprovider`
    - extra_test_file 给定时：先拷贝到 <repo>/tests/<hidden_name> 再运行（历史+隐藏一起跑）。
    - 返回 dict：{"exit_code", "passed", "total", "ok", "log_text",
                 "history": {"passed","total"}, "hidden": {"passed","total"}, "hidden_dest"}
    - log_text 仅保存在服务端/CLI 输出，绝不进入 Web 前端（防测试内容泄漏）。
    """
    cfg = load_config()
    repo = Path(repo_path)
    tests_dir = repo / cfg.get("history_tests_dir", "tests")
    hidden_dest: Optional[Path] = None

    if extra_test_file is not None:
        src = Path(extra_test_file)
        if not src.exists():
            return {"exit_code": -1, "passed": 0, "total": 0, "ok": False,
                    "log_text": "extra_test_file 不存在: %s" % src,
                    "history": {"passed": 0, "total": 0}, "hidden": {"passed": 0, "total": 0},
                    "hidden_dest": None, "error": "本轮隐藏测试文件缺失"}
        tests_dir.mkdir(parents=True, exist_ok=True)
        hidden_dest = tests_dir / (hidden_name or src.name)
        shutil.copy2(src, hidden_dest)

    fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="bugrelay_junit_")
    os.close(fd)
    cmd = [sys.executable, "-m", "pytest", *cfg.get("pytest_args", ["-q"]),
           "--junitxml=%s" % xml_path, "-o", "junit_family=legacy",
           "--tb=short", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # 帮助 "from src import ..." 风格的测试能直接跑（不改 arena 任何文件）
    env["PYTHONPATH"] = os.pathsep.join([str(repo)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    log_text = ""
    exit_code = -1
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                              timeout=PYTEST_TIMEOUT_SECONDS, env=env)
        exit_code = proc.returncode
        log_text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:20000]
    except subprocess.TimeoutExpired:
        log_text = "pytest 运行超时（>%ds），判定 FAIL" % PYTEST_TIMEOUT_SECONDS
        exit_code = -1
    except Exception as e:
        log_text = "pytest 启动失败: %s" % e
        exit_code = -1

    hname = hidden_dest.name if hidden_dest else None
    stats = _parse_junit(xml_path, hname)
    if stats is None:
        stats = _parse_summary_fallback(log_text, hname)
    try:
        os.unlink(xml_path)
    except Exception:
        pass

    total = stats["history"]["total"] + stats["hidden"]["total"]
    passed = stats["history"]["passed"] + stats["hidden"]["passed"]
    # 全绿判定：exit_code==0（pytest 严格语义：无 fail / 无 error）。exit 5（无测试）不算绿。
    ok = (exit_code == 0) and (total > 0)

    return {
        "exit_code": exit_code,
        "passed": passed,
        "total": total,
        "ok": ok,
        "log_text": log_text,
        "history": {"passed": stats["history"]["passed"], "total": stats["history"]["total"]},
        "hidden": {"passed": stats["hidden"]["passed"], "total": stats["hidden"]["total"]},
        "hidden_dest": str(hidden_dest) if hidden_dest else None,
    }


# ---------------------------------------------------------------------------
# 备份 / 还原
# ---------------------------------------------------------------------------

def _backups_dir() -> Path:
    cfg = load_config()
    return resolve_path(cfg.get("backups_dir", "backups"))


def _backup_index_path() -> Path:
    return _backups_dir() / "index.json"


def _load_backup_index() -> List[Dict]:
    try:
        if _backup_index_path().exists():
            with open(_backup_index_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("backups", []) if isinstance(data, dict) else []
    except Exception:
        pass
    return []


def backup_arena(tag: str) -> Optional[str]:
    """把 arena_repo 当前完整状态备份到 backups/（规范【6】）。

    方式：完整目录复制（保留 .git，忽略 __pycache__ 等噪声），并在
    backups/index.json 登记 {id, tag, ts, commit}。返回备份 id；失败返回 None。
    """
    arena = arena_path()
    if not repo_ops.is_arena_ready():
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    bid = "%s_%s" % (ts, re.sub(r"[^A-Za-z0-9_.-]", "_", tag))
    dst = _backups_dir() / bid
    try:
        _backups_dir().mkdir(parents=True, exist_ok=True)
        shutil.copytree(arena, dst,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".DS_Store"))
    except Exception as e:
        log_event("backup", "备份失败: %s" % e, result="WARN")
        return None
    entry = {"id": bid, "tag": tag, "ts": now_iso(),
             "commit": repo_ops.current_commit(arena), "path": str(dst)}
    index = _load_backup_index()
    index.append(entry)
    try:
        with open(_backup_index_path(), "w", encoding="utf-8") as f:
            json.dump({"backups": index}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    log_event("backup", "已备份 arena_repo -> backups/%s (tag=%s)" % (bid, tag))
    return bid


def latest_backup() -> Optional[Dict]:
    """返回最近一次可用备份的登记信息（目录必须仍存在）。"""
    for entry in reversed(_load_backup_index()):
        if entry.get("path") and Path(entry["path"]).exists():
            return entry
    return None


def restore_arena() -> Dict:
    """从 backups/ 最近一次备份还原 arena_repo（规范【6】）。

    - 有备份：清空 arena 当前内容（含未跟踪临时文件），再完整复制备份回去
      （历史测试包含在备份内，因此"保留历史测试"天然满足）；
    - 无备份：git 兜底（checkout 已跟踪文件 + clean 未跟踪，但排除 tests/，
      避免误删人类手放、尚未 commit 的初始历史测试）；
    - 两者都不可用：返回失败（不抛异常）。
    """
    arena = arena_path()
    cfg = load_config()
    if not arena.exists():
        return {"ok": False, "error": "arena_repo 不存在，无法还原"}
    if not repo_ops.is_arena_ready():
        return {"ok": False, "error": "arena_repo 未就绪（不是可用的 git 仓库或路径非法）"}

    entry = latest_backup()
    if entry is not None:
        try:
            repo_ops.clean_dir_contents(arena)
            shutil.copytree(Path(entry["path"]), arena, dirs_exist_ok=True)
            log_event("restore", "已从备份 %s 还原 arena_repo" % entry["id"])
            return {"ok": True, "backup_id": entry["id"], "message": "已还原到备份 %s" % entry["id"]}
        except Exception as e:
            log_event("restore", "从备份还原失败: %s" % e, result="WARN")
            return {"ok": False, "error": "还原失败: %s" % e}

    # git 兜底
    tdir = cfg.get("history_tests_dir", "tests")
    try:
        repo_ops.git(arena, "checkout", "--", ".")
        repo_ops.git(arena, "clean", "-fdx", "-e", tdir + "/", "-e", tdir)
        log_event("restore", "无备份，已用 git 还原（保留 %s/ 下未跟踪文件）" % tdir)
        return {"ok": True, "backup_id": None, "message": "无备份，已用 git 还原（保留 %s/）" % tdir}
    except Exception as e:
        return {"ok": False, "error": "git 还原失败: %s" % e}


def apply_business_files(upload_path: str | Path) -> Dict:
    """将上传包解压/复制到 arena_repo/business_dir（规范【6】，严格排除 tests/）。"""
    return repo_ops.apply_upload_to_business(upload_path)


# ---------------------------------------------------------------------------
# 材料导入（Web 上传 / CLI load-* 共用）
# ---------------------------------------------------------------------------

def import_answer(path_str: str) -> Dict:
    """导入答题材料（等同 POST /api/answer）。

    接受 .zip 文件、目录、或单个业务文件；统一登记到 state.pending_answer。
    """
    p = Path(path_str)
    if not p.exists():
        return {"ok": False, "error": "路径不存在: %s" % p}
    pending: Path
    if p.is_file() and p.suffix.lower() == ".zip":
        pending = p
    elif p.is_dir():
        pending = p
    elif p.is_file():
        stage = UPLOADS_DIR / ("answer_%s" % uuid.uuid4().hex[:8])
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, stage / p.name)
        pending = stage
    else:
        return {"ok": False, "error": "无法识别的答题材料: %s" % p}

    state = load_state()
    state["pending_answer"] = str(pending)
    _set_msg(state, "已导入答题材料（等待「验收答题」）")
    save_state(state)
    log_event("import-answer", "导入答题材料: %s" % pending.name, player=state.get("current_player"),
              round_=state.get("round"))
    return {"ok": True, "pending": str(pending), "message": "已导入答题材料: %s" % pending.name}


def import_proposal(prompt_file: str, test_file: str) -> Dict:
    """导入出题材料（等同 POST /api/proposal）：next_prompt.md + hidden_tests.py。"""
    pf, tf = Path(prompt_file), Path(test_file)
    if not pf.exists() or not tf.exists():
        return {"ok": False, "error": "需求文档或隐藏测试文件不存在"}
    try:
        prompt_text = pf.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": "需求文档读取失败: %s" % e}
    if not prompt_text.strip():
        return {"ok": False, "error": "需求文档为空"}
    if tf.suffix.lower() != ".py":
        return {"ok": False, "error": "隐藏测试必须是 .py 文件"}

    stage = UPLOADS_DIR / ("proposal_%s" % uuid.uuid4().hex[:8])
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pf, stage / "next_prompt.md")
    shutil.copy2(tf, stage / "hidden_tests.py")

    state = load_state()
    state["pending_proposal"] = {"prompt": str(stage / "next_prompt.md"),
                                 "test": str(stage / "hidden_tests.py")}
    _set_msg(state, "已导入出题材料（等待「校验出题并交棒」）")
    save_state(state)
    log_event("import-proposal", "导入出题材料（需求文档 + 隐藏测试）",
              player=state.get("current_player"), round_=state.get("round"))
    return {"ok": True, "pending": state["pending_proposal"],
            "message": "已导入出题材料（需求 + 隐藏测试），等待校验"}


# ---------------------------------------------------------------------------
# inbox 材料投递目录：自动拾取（免手动上传/复制粘贴）
#
# 约定（Agent 交付物按固定文件名写入 inbox/，人类按按钮时框架自动读取）：
#   答题材料：inbox/answer.zip（压缩包）或 inbox/answer/（目录）
#   出题材料：inbox/next_prompt.md + inbox/hidden_tests.py
# 已消费的材料自动移入 inbox/_consumed/<时间戳>/ 留档，避免下轮误拾取。
# inbox_dir 可在 config.json 配置——可直接指向 Agent 的产物输出目录。
# ---------------------------------------------------------------------------

def inbox_status() -> Dict:
    """检测 inbox/ 中是否已就位本轮材料（供 Web/CLI 提示与按钮启用）。"""
    inbox = inbox_dir_path()
    return {
        "answer": (inbox / "answer.zip").is_file() or (inbox / "answer").is_dir(),
        "proposal": (inbox / "next_prompt.md").is_file() and (inbox / "hidden_tests.py").is_file(),
    }


def _archive_inbox(items: List[Path]) -> None:
    """把已消费的 inbox 材料移入 inbox/_consumed/<时间戳>/ 留档。"""
    try:
        dest = inbox_dir_path() / "_consumed" / time.strftime("%Y%m%d_%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        for it in items:
            if it.exists():
                shutil.move(str(it), str(dest / it.name))
    except Exception as e:
        log_event("inbox", "材料归档失败: %s" % e, result="WARN")


def pickup_answer() -> Dict:
    """从 inbox/ 自动拾取答题材料（复制到暂存后登记 pending，原件归档）。"""
    inbox = inbox_dir_path()
    src: Optional[Path] = None
    if (inbox / "answer.zip").is_file():
        src = inbox / "answer.zip"
    elif (inbox / "answer").is_dir():
        src = inbox / "answer"
    if src is None:
        return {"ok": False, "error": "inbox/ 中未发现答题材料（约定：inbox/answer.zip 或 inbox/answer/ 目录）"}

    stage = UPLOADS_DIR / ("answer_%s" % uuid.uuid4().hex[:8])
    try:
        if src.is_file():
            stage.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, stage / "answer.zip")
            target: Path = stage / "answer.zip"
        else:
            shutil.copytree(src, stage, dirs_exist_ok=True)
            target = stage
    except Exception as e:
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "error": "复制 inbox 答题材料失败: %s" % e}

    result = import_answer(str(target))
    if result.get("ok"):
        log_event("inbox", "已从 inbox/ 自动拾取答题材料: %s" % src.name)
        _archive_inbox([src])
    else:
        shutil.rmtree(stage, ignore_errors=True)
    return result


def pickup_proposal() -> Dict:
    """从 inbox/ 自动拾取出题材料（next_prompt.md + hidden_tests.py）。"""
    inbox = inbox_dir_path()
    pf, tf = inbox / "next_prompt.md", inbox / "hidden_tests.py"
    if not (pf.is_file() and tf.is_file()):
        return {"ok": False, "error": "inbox/ 中未发现完整出题材料（约定：next_prompt.md + hidden_tests.py）"}
    result = import_proposal(str(pf), str(tf))
    if result.get("ok"):
        log_event("inbox", "已从 inbox/ 自动拾取出题材料（需求 + 隐藏测试）")
        _archive_inbox([pf, tf])
    return result


# ---------------------------------------------------------------------------
# 答题验收（不调用任何模型）
# ---------------------------------------------------------------------------

def verify_answer() -> Dict:
    """验收当前选手的答题（规范【6】）。

    流程：前置检查 -> 备份当前 -> tests 状态快照(前) -> 应用业务文件 ->
    tests 状态快照(后) 对比（篡改即 FAIL 并回滚）-> 把本轮隐藏测试拷入 tests/ ->
    跑 pytest（历史+隐藏）-> 全绿则隐藏测试转正+再备份+phase=proposing；
    否则回滚+淘汰+切换下一位。全程不调用验题模型。
    """
    cfg = load_config()
    with VERIFY_LOCK:
        state = load_state()
        player = state.get("current_player")
        rnd = state.get("round", 1)

        # ---- 前置检查（不动 arena、不动选手） ----
        if state.get("status") == "finished":
            return {"ok": False, "result": None, "reason": "比赛已结束"}
        if not repo_ops.is_arena_ready():
            state = load_state()
            _set_msg(state, "arena_repo 未就绪，无法验收答题")
            save_state(state)
            return {"ok": False, "result": None, "reason": "arena_repo 未就绪（不存在或不是 git 仓库）"}
        if state.get("phase") != "answering":
            return {"ok": False, "result": None, "reason": "当前不是答题阶段（phase=%s）" % state.get("phase")}
        pending = state.get("pending_answer")
        if not pending or not Path(pending).exists():
            # inbox 自动拾取：材料已放入 inbox/ 时无需手动上传
            picked = pickup_answer()
            if picked.get("ok"):
                state = load_state()
                pending = state.get("pending_answer")
            else:
                return {"ok": False, "result": None,
                        "reason": "尚未导入答题材料：请上传（/api/answer / load-answer），或把材料放入 "
                                  "inbox/（answer.zip 或 answer/ 目录）后重试"}

        hidden_dir = resolve_path(cfg.get("hidden_tests_dir", "hidden_tests"))
        hidden_src = hidden_dir / "hidden_tests.py"
        if not hidden_src.exists():
            return {"ok": False, "result": None,
                    "reason": "本轮隐藏测试缺失：请人工将本轮 hidden_tests.py 放入 hidden_tests/ 后重试"}

        arena = arena_path()
        tests_dir = arena / cfg.get("history_tests_dir", "tests")

        # ---- 1. 备份当前（保底，失败可还原） ----
        backup_arena("pre_answer_r%s_%s" % (rnd, player))

        def _fail(reason: str, eliminate: bool = True) -> Dict:
            """统一失败收尾：回滚 + （默认）淘汰 + 切换下一位。"""
            restore_arena()
            state2 = load_state()
            if eliminate:
                _advance(state2, eliminate_current=True)
                log_event("judge-answer", "选手 %s 答题 FAIL：%s" % (player, reason),
                          result="FAIL", player=player, round_=rnd)
            state2["last_result"] = "FAIL"
            state2["phase"] = "answering"
            state2["pending_answer"] = None
            state2["last_test_summary"] = None
            _set_msg(state2, "选手 %s 答题失败：%s" % (player, reason))
            save_state(state2)
            return {"ok": True, "result": "FAIL", "reason": reason, "player": player,
                    "round": rnd, "eliminated": eliminate,
                    "next_player": state2.get("current_player"),
                    "message": "选手 %s 答题失败（%s），已回滚并切换" % (player, reason)}

        # ---- 2. 应用前 tests 快照 ----
        before = repo_ops.tests_status_map()

        # ---- 3. 应用业务文件（只进 business_dir，严禁触碰 tests/） ----
        applied = apply_business_files(pending)
        if not applied.get("ok"):
            return _fail("答题材料无效：%s" % applied.get("error", "应用失败"))
        if applied.get("warning"):
            log_event("apply", applied["warning"], result="WARN", player=player, round_=rnd)

        # ---- 4. 应用后 tests 快照对比（篡改历史测试 -> 立即回滚并判 FAIL） ----
        after = repo_ops.tests_status_map()
        if before != after:
            return _fail("检测到历史测试被改动（篡改 tests/），已回滚")

        # ---- 5. 拷入本轮隐藏测试，跑 pytest（历史+隐藏） ----
        hidden_name = "test_hidden_r%s_%s.py" % (rnd, player)
        res = run_pytest(arena, extra_test_file=hidden_src, hidden_name=hidden_name)
        if res.get("log_text"):
            # pytest 原始输出只留在服务端终端（CLI 场景给人类排障），不进前端、不进日志
            print("[pytest r%s %s] exit=%s passed=%s total=%s\n%s" %
                  (rnd, player, res.get("exit_code"), res.get("passed"), res.get("total"),
                   res.get("log_text", "")[-4000:]), file=sys.stderr)

        if not res.get("ok"):
            hidden_dest = res.get("hidden_dest")
            if hidden_dest and Path(hidden_dest).exists():
                try:
                    Path(hidden_dest).unlink()
                except Exception:
                    pass
            counts = _summary_counts(res)
            detail = "历史 %s/%s，隐藏 %s/%s 未全绿" % (
                res["history"]["passed"], res["history"]["total"],
                res["hidden"]["passed"], res["hidden"]["total"])
            state2 = load_state()
            state2["last_test_summary"] = counts
            save_state(state2)
            return _fail(detail)

        # ---- 6. 全绿：隐藏测试永久转正 + 成功点备份 + 等待该选手出题 ----
        hidden_dest = Path(res["hidden_dest"]) if res.get("hidden_dest") else None
        if hidden_dest and hidden_dest.exists():
            final_name = "test_round_%s_%s.py" % (rnd, player)
            final_path = tests_dir / final_name
            if final_path.exists():
                final_path = tests_dir / ("test_round_%s_%s_%s.py" % (rnd, player, uuid.uuid4().hex[:6]))
            shutil.move(str(hidden_dest), str(final_path))
            # 暂存目录清空（评测后清理/归档：已归档进 arena tests/）
            for extra in hidden_dir.glob("*.py"):
                if extra.name != "hidden_tests.py":
                    extra.unlink()
            try:
                (hidden_dir / "hidden_tests.py").unlink()
            except Exception:
                pass

        backup_arena("answer_ok_r%s_%s" % (rnd, player))

        counts = _summary_counts(res)
        state2 = load_state()
        state2["phase"] = "proposing"  # 轮次不变，等待该选手出题
        state2["scores"] = dict(state2.get("scores", {}))
        state2["scores"][player] = state2["scores"].get(player, 0) + 1
        state2["last_result"] = "PASS"
        state2["last_test_summary"] = counts
        state2["pending_answer"] = None
        _set_msg(state2, "选手 %s 答题通过（历史 %s/%s，隐藏 %s/%s），请提交出题材料" % (
            player, res["history"]["passed"], res["history"]["total"],
            res["hidden"]["passed"], res["hidden"]["total"]))
        save_state(state2)
        log_event("judge-answer", "选手 %s 答题 PASS：历史 %s/%s，隐藏 %s/%s，隐藏测试已转为历史" % (
            player, res["history"]["passed"], res["history"]["total"],
            res["hidden"]["passed"], res["hidden"]["total"]),
            result="PASS", player=player, round_=rnd)
        return {"ok": True, "result": "PASS", "player": player, "round": rnd, "eliminated": False,
                "history": counts["history"], "hidden": counts["hidden"],
                "message": "选手 %s 答题通过，请继续出题（提交需求 + 隐藏测试）" % player}


# ---------------------------------------------------------------------------
# 验题模型（唯一允许的模型调用，只调一次）
# ---------------------------------------------------------------------------

def call_verifier_once(prompt_text: str) -> Tuple[bool, Optional[str], str]:
    """单次调用 verifier_model（OpenAI 兼容 /chat/completions）。不重试。

    返回 (ok, content, err)。超时按 timeout_seconds 直接判失败（由调用方判 FAIL）。
    """
    cfg = load_config()
    vm = cfg.get("verifier_model", {})
    timeout = float(vm.get("timeout_seconds", 600))
    url = str(vm.get("base_url", "")).rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer %s" % vm.get("api_key", ""), "Content-Type": "application/json"}
    payload = {
        "model": vm.get("model", ""),
        "stream": False,
        "messages": [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
    }
    try:
        import httpx  # 延迟导入：未装 httpx 不影响其他功能
    except Exception as e:
        return False, None, "httpx 未安装（pip install -r requirements.txt）: %s" % e
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            return False, None, "模型返回内容为空"
        return True, content, ""
    except Exception as e:
        return False, None, "verifier_model 调用失败（不重试，%ss 超时即 FAIL）: %s: %s" % (
            int(timeout), type(e).__name__, e)


def parse_model_files(text: str) -> Optional[List[Tuple[str, str]]]:
    """解析验题模型输出（规范【8】）。

    提取所有 "=== FILE: 相对路径 ===" 块，内容为其后直到下一个块头/结尾的文本。
    一个文件块都没有 -> 返回 None（判 FAIL）。
    """
    matches = list(_FILE_HEADER_RE.finditer(text))
    if not matches:
        return None
    files: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        rel = m.group(1).strip().strip('"').strip("'").strip()
        rel = rel.lstrip("./").replace("\\", "/")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end]
        # 剥掉模型违规加的 Markdown 围栏（如果整体被 ``` 包住）
        c = content.strip("\r\n")
        if c.startswith("```") and c.rstrip().endswith("```"):
            first_nl = c.find("\n")
            if first_nl != -1:
                c = c[first_nl + 1:c.rstrip().rfind("```")].rstrip("\n")
        files.append((rel, c))
    return files if files else None


def _write_model_output(tmp_repo: Path, files: List[Tuple[str, str]], cfg: Dict) -> Tuple[int, List[str]]:
    """把模型输出的文件块安全写入临时目录。

    返回 (写入数, 被跳过的路径列表)。跳过规则：绝对路径 / 路径穿越 / 目标在
    历史测试目录下（模型只许实现业务代码，不许动测试）。
    """
    tdir = cfg.get("history_tests_dir", "tests")
    written, skipped = 0, []
    root = tmp_repo.resolve()
    for rel, content in files:
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            skipped.append(rel)
            continue
        if parts[0] == tdir:
            skipped.append(rel)
            continue
        dest = (tmp_repo / Path(*parts)).resolve()
        if not str(dest).startswith(str(root) + os.sep):
            skipped.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written += 1
    return written, skipped


# ---------------------------------------------------------------------------
# 出题验题（自证）并交棒
# ---------------------------------------------------------------------------

def verify_proposal() -> Dict:
    """校验出题并交棒（规范【6】，框架中唯一调用模型的环节）。

    流程：读材料 -> 建临时目录 tmp/relay_<uuid> -> 复制 arena 全量（排除
    .git/hidden_tests/临时文件）-> 单次调 verifier_model 仅凭需求重新实现 ->
    解析 === FILE === 块写入临时目录（解析失败即 FAIL，不重试）-> 拷入选手
    hidden_tests.py -> 临时目录跑 pytest（历史+隐藏）-> 全绿：保存 prompt 到
    prompts/、新 hidden 落 hidden_tests/、round+1、交棒；否则：回滚（恢复最近
    备份，即出题者答题通过后的状态，其代码依规则保留）、出题者淘汰、切换下一位。
    """
    cfg = load_config()
    with VERIFY_LOCK:
        state = load_state()
        player = state.get("current_player")
        rnd = state.get("round", 1)

        # ---- 前置检查 ----
        if state.get("status") == "finished":
            return {"ok": False, "result": None, "reason": "比赛已结束"}
        if not repo_ops.is_arena_ready():
            state = load_state()
            _set_msg(state, "arena_repo 未就绪，无法校验出题")
            save_state(state)
            return {"ok": False, "result": None, "reason": "arena_repo 未就绪（不存在或不是 git 仓库）"}
        if state.get("phase") != "proposing":
            return {"ok": False, "result": None,
                    "reason": "当前选手尚未通过答题验收（phase=%s）" % state.get("phase")}
        pending = state.get("pending_proposal") or {}
        prompt_path = Path(pending.get("prompt", "")) if pending.get("prompt") else None
        test_path = Path(pending.get("test", "")) if pending.get("test") else None
        if not (prompt_path and test_path and prompt_path.exists() and test_path.exists()):
            # inbox 自动拾取：出题材料已放入 inbox/ 时无需手动上传
            picked = pickup_proposal()
            if picked.get("ok"):
                state = load_state()
                pending = state.get("pending_proposal") or {}
                prompt_path = Path(pending.get("prompt", "")) if pending.get("prompt") else None
                test_path = Path(pending.get("test", "")) if pending.get("test") else None
            if not (prompt_path and test_path and prompt_path.exists() and test_path.exists()):
                return {"ok": False, "result": None,
                        "reason": "尚未导入出题材料：请上传（/api/proposal / load-proposal），或把 "
                                  "next_prompt.md 与 hidden_tests.py 放入 inbox/ 后重试"}
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            return {"ok": False, "result": None, "reason": "需求文档读取失败: %s" % e}
        if not prompt_text.strip():
            return {"ok": False, "result": None, "reason": "需求文档为空"}

        arena = arena_path()
        tmp_root = BASE_DIR / "tmp"
        relay_dir = tmp_root / ("relay_%s" % uuid.uuid4().hex[:12])
        tmp_repo = relay_dir / "repo"

        def _cleanup_tmp() -> None:
            shutil.rmtree(relay_dir, ignore_errors=True)

        def _fail(reason: str, res: Optional[Dict] = None) -> Dict:
            """出题失败收尾：回滚（恢复最近成功备份）+ 出题者淘汰 + 切换下一位。

            注意：最近一次成功备份即"该选手答题通过"时的状态，因此其已通过验收的
            业务代码依赛制保留，成为后人的环境；被拒绝的只是本轮新需求。
            """
            _cleanup_tmp()
            restore_arena()
            state2 = load_state()
            _advance(state2, eliminate_current=True)
            state2["last_result"] = "FAIL"
            state2["phase"] = "answering"
            state2["pending_proposal"] = None
            if res is not None:
                state2["last_test_summary"] = _summary_counts(res)
            _set_msg(state2, "选手 %s 出题失败：%s" % (player, reason))
            save_state(state2)
            log_event("judge-proposal", "选手 %s 出题 FAIL：%s" % (player, reason),
                      result="FAIL", player=player, round_=rnd)
            return {"ok": True, "result": "FAIL", "reason": reason, "player": player,
                    "round": rnd, "eliminated": True,
                    "next_player": state2.get("current_player"),
                    "message": "选手 %s 出题失败（%s），已回滚并切换" % (player, reason)}

        try:
            # ---- 1. 临时目录 + 复制 arena 全量 ----
            relay_dir.mkdir(parents=True, exist_ok=True)
            try:
                repo_ops.copy_arena_to(tmp_repo, for_verify=True)
            except Exception as e:
                return _fail("复制 arena_repo 失败: %s" % e)

            # ---- 2. 单次调用验题模型（全框架唯一的模型调用） ----
            ok, content, err = call_verifier_once(prompt_text)
            if not ok:
                return _fail("验题模型不可用或超时 -> %s" % err)

            # ---- 3. 解析 === FILE === 块（无任何块即 FAIL，不重试） ----
            files = parse_model_files(content or "")
            if not files:
                return _fail("模型输出未包含任何 === FILE: ... === 文件块（格式非法）")

            # ---- 4. 模型输出写入临时目录（禁止写 tests/） ----
            written, skipped = _write_model_output(tmp_repo, files, cfg)
            if written == 0:
                return _fail("模型输出没有可写入的合法文件（全部被安全规则跳过）")
            if skipped:
                log_event("judge-proposal", "模型试图写 tests/ 或非法路径，已跳过 %d 个块" % len(skipped),
                          result="WARN", player=player, round_=rnd)

            # ---- 5. 选手隐藏测试拷入临时目录 tests/ ----
            hidden_name = "test_hidden_proposal_%s.py" % uuid.uuid4().hex[:8]

            # ---- 6. 临时目录跑 pytest（历史+隐藏） ----
            res = run_pytest(tmp_repo, extra_test_file=test_path, hidden_name=hidden_name)
            if res.get("log_text"):
                print("[pytest proposal r%s %s] exit=%s passed=%s total=%s\n%s" %
                      (rnd, player, res.get("exit_code"), res.get("passed"), res.get("total"),
                       res.get("log_text", "")[-4000:]), file=sys.stderr)
            if not res.get("ok"):
                return _fail("验题实现未让全部测试通过（历史 %s/%s，隐藏 %s/%s）" % (
                    res["history"]["passed"], res["history"]["total"],
                    res["hidden"]["passed"], res["hidden"]["total"]), res)

            # ---- 7. 合法：保存需求 + 新一轮隐藏测试 + 交棒 ----
            prompts_dir = resolve_path(cfg.get("prompts_dir", "prompts"))
            prompts_dir.mkdir(parents=True, exist_ok=True)
            new_round = rnd + 1
            prompt_name = "round_%s_by_%s.md" % (new_round, player)
            (prompts_dir / prompt_name).write_text(prompt_text, encoding="utf-8")

            hidden_dir = resolve_path(cfg.get("hidden_tests_dir", "hidden_tests"))
            hidden_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(test_path, hidden_dir / "hidden_tests.py")  # 供下一轮作为"本轮隐藏测试"

            counts = _summary_counts(res)
            state2 = load_state()
            state2["current_prompt_file"] = prompt_name
            state2["round"] = new_round
            _advance(state2, eliminate_current=False)  # 交棒：出题者保留，切换下一位
            state2["phase"] = "answering"
            state2["scores"] = dict(state2.get("scores", {}))
            state2["scores"][player] = state2["scores"].get(player, 0) + 1
            state2["last_result"] = "PASS"
            state2["last_test_summary"] = counts
            state2["pending_proposal"] = None
            _set_msg(state2, "选手 %s 出题合法，已交棒（第 %s 轮，需求 %s）" % (
                player, new_round, prompt_name))
            save_state(state2)
            log_event("judge-proposal", "选手 %s 出题 PASS：验题自证通过（历史 %s/%s，隐藏 %s/%s），交棒 -> 第 %s 轮" % (
                player, res["history"]["passed"], res["history"]["total"],
                res["hidden"]["passed"], res["hidden"]["total"], new_round),
                result="PASS", player=player, round_=rnd)
            return {"ok": True, "result": "PASS", "player": player, "round": rnd,
                    "new_round": new_round, "eliminated": False,
                    "next_player": state2.get("current_player"),
                    "history": counts["history"], "hidden": counts["hidden"],
                    "message": "出题合法，已交棒：第 %s 轮，当前选手 %s" % (new_round, state2.get("current_player"))}
        finally:
            _cleanup_tmp()
