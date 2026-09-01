"""Bug Relay 裁判核心（规范【6】）。

本模块是唯一会修改 arena_repo 的地方，且只在人类触发（Web 按钮 / CLI 命令）的瞬间工作：
导入文件 -> 跑 pytest -> 记录结果 -> 备份/回滚。框架不启动、不调用、不观测任何
AI Agent；AI 或真人都只是在框架外产出文件。

主要函数：
- run_pytest(repo_path, extra_test_file=None, hidden_name=None):
    在指定仓库运行 pytest（config.pytest_args）。extra_test_file 给定时先拷入
    目标 tests/ 再跑（历史+隐藏）。返回 dict（含 exit_code/passed/total/log_text，
    以及 history/hidden 的分别计数）。
- backup_arena(tag): 把 arena 当前完整状态复制到 backups/，登记 index.json，返回备份 id。
- restore_arena(): 从最近一次备份还原（无备份时用 git checkout+clean 兜底，保留 tests/）。
- apply_business_files(upload_path): 见 repo_ops.apply_upload_to_business（严格排除 tests/）。
- verify_answer(): 答题验收，要求历史测试 + 本轮隐藏测试全部 GREEN。
- verify_proposal(): 两次推进的手动自证状态机：第一次要求新测试全部 RED 并创建
  独立工作区；人类在该目录手动运行同模型 OpenCode（或真人编码）后，第二次要求
  历史测试 + 新测试全部 GREEN。自证代码只用于证明题目可解，不进入正式 arena。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import random
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

# 全局裁判锁：防止 Web 双击 / CLI 并发导致两次评测同时改 arena。
# 使用 RLock，给内部失败收尾/恢复路径保留可重入空间。
VERIFY_LOCK = threading.RLock()

# pytest 单次运行的保护超时（防止测试死循环卡死框架）
PYTEST_TIMEOUT_SECONDS = 1800

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


def _summary_counts(res: Dict, overall: Optional[str] = None) -> Dict:
    """把 run_pytest 结果压成前端可展示的两行计数（不含任何测试内容）。"""
    def pack(part: Dict) -> Dict:
        passed = int(part.get("passed", 0) or 0)
        total = int(part.get("total", 0) or 0)
        failed = int(part.get("failed", 0) or 0)
        errors = int(part.get("errors", 0) or 0)
        skipped = int(part.get("skipped", 0) or 0)
        return {"passed": passed, "total": total, "failed": failed,
                "errors": errors, "skipped": skipped,
                "ok": bool(total > 0 and passed == total and failed == errors == skipped == 0)}
    return {"history": pack(res.get("history", {})), "hidden": pack(res.get("hidden", {})),
            "overall": overall or ("PASS" if res.get("ok") else "FAIL")}


# ---------------------------------------------------------------------------
# pytest 运行
# ---------------------------------------------------------------------------

def _parse_junit(xml_path: str, hidden_name: Optional[str]) -> Optional[Dict]:
    """解析 pytest junit xml，按文件归出 history / hidden 两份计数。

    使用 junit_family=legacy（testcase 带 file 属性）。解析失败返回 None（走 fallback）。
    计数口径严格区分 passed / failed / errors / skipped。Bug Relay 的 RED/GREEN
    闸门不把 skipped 当作通过，也不把 collection/import error 当作有效 RED。
    """
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return None
    hist = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    hid = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    for tc in root.iter("testcase"):
        f = (tc.get("file") or "").replace("\\", "/")
        is_hidden = bool(hidden_name) and (os.path.basename(f) == hidden_name or f.endswith("/" + (hidden_name or "\x00")))
        bucket = hid if is_hidden else hist
        bucket["total"] += 1
        if tc.find("failure") is not None:
            bucket["failed"] += 1
        elif tc.find("error") is not None:
            bucket["errors"] += 1
        elif tc.find("skipped") is not None:
            bucket["skipped"] += 1
        else:
            bucket["passed"] += 1
    return {"history": hist, "hidden": hid}


def _parse_summary_fallback(stdout: str, hidden_name: Optional[str]) -> Dict:
    """从 -q 输出末行解析计数（junit xml 不可用时的兜底，无法分离 history/hidden）。"""
    passed = failed = errors = skipped = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error|errors|skipped)", stdout):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        elif kind == "skipped":
            skipped = n
        else:
            errors = n
    total = passed + failed + errors + skipped
    empty = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    hist = dict(empty) if hidden_name else {
        "passed": passed, "failed": failed, "errors": errors, "skipped": skipped, "total": total}
    hid = {"passed": passed, "failed": failed, "errors": errors,
           "skipped": skipped, "total": total} if hidden_name else dict(empty)
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
                    "history": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0},
                    "hidden": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0},
                    "hidden_dest": None, "stats_reliable": False,
                    "error": "本轮隐藏测试文件缺失"}
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
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
    stats_reliable = stats is not None
    if not stats_reliable:
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
        "history": dict(stats["history"]),
        "hidden": dict(stats["hidden"]),
        "hidden_dest": str(hidden_dest) if hidden_dest else None,
        "stats_reliable": stats_reliable,
    }


def _proposal_test_count(cfg: Optional[Dict] = None) -> int:
    cfg = cfg or load_config()
    try:
        count = int(cfg.get("proposal_test_count", 3))
    except (TypeError, ValueError):
        count = 3
    return max(count, 1)


def _part_is_green(part: Dict, allow_empty: bool = True) -> bool:
    total = int(part.get("total", 0) or 0)
    if not allow_empty and total == 0:
        return False
    return (
        int(part.get("passed", 0) or 0) == total
        and int(part.get("failed", 0) or 0) == 0
        and int(part.get("errors", 0) or 0) == 0
        and int(part.get("skipped", 0) or 0) == 0
    )


def _validate_all_green(res: Dict, expected_hidden: int) -> Tuple[bool, str]:
    """严格 GREEN：历史无失败/错误/跳过；隐藏恰好 N 条且全部真实通过。"""
    if not res.get("stats_reliable"):
        return False, "JUnit 结果不可用，无法可靠区分历史与隐藏测试"
    history = res.get("history", {})
    hidden = res.get("hidden", {})
    if not _part_is_green(history, allow_empty=True):
        return False, "历史测试未全绿（通过 %s/%s，失败 %s，错误 %s，跳过 %s）" % (
            history.get("passed", 0), history.get("total", 0), history.get("failed", 0),
            history.get("errors", 0), history.get("skipped", 0))
    if int(hidden.get("total", 0) or 0) != expected_hidden:
        return False, "本轮测试必须实际收集 %d 条（当前 %s 条）" % (
            expected_hidden, hidden.get("total", 0))
    if not _part_is_green(hidden, allow_empty=False) or res.get("exit_code") != 0:
        return False, "本轮测试未全绿（通过 %s/%s，失败 %s，错误 %s，跳过 %s）" % (
            hidden.get("passed", 0), hidden.get("total", 0), hidden.get("failed", 0),
            hidden.get("errors", 0), hidden.get("skipped", 0))
    return True, ""


def _validate_all_red(res: Dict, expected_hidden: int) -> Tuple[bool, str]:
    """严格 RED：历史仍全绿；隐藏恰好 N 条且每条都是断言失败。"""
    if not res.get("stats_reliable"):
        return False, "JUnit 结果不可用，无法可靠区分历史与新测试"
    history = res.get("history", {})
    hidden = res.get("hidden", {})
    if not _part_is_green(history, allow_empty=True):
        return False, "历史测试必须保持全绿（通过 %s/%s，失败 %s，错误 %s，跳过 %s）" % (
            history.get("passed", 0), history.get("total", 0), history.get("failed", 0),
            history.get("errors", 0), history.get("skipped", 0))
    if int(hidden.get("total", 0) or 0) != expected_hidden:
        return False, "新测试必须实际收集 %d 条（当前 %s 条）" % (
            expected_hidden, hidden.get("total", 0))
    if not (
        int(hidden.get("failed", 0) or 0) == expected_hidden
        and int(hidden.get("passed", 0) or 0) == 0
        and int(hidden.get("errors", 0) or 0) == 0
        and int(hidden.get("skipped", 0) or 0) == 0
        and res.get("exit_code") == 1
    ):
        return False, "新测试必须全部 RED（通过 %s，失败 %s/%s，错误 %s，跳过 %s）" % (
            hidden.get("passed", 0), hidden.get("failed", 0), hidden.get("total", 0),
            hidden.get("errors", 0), hidden.get("skipped", 0))
    return True, ""


_MANIFEST_IGNORE_NAMES = {".git", "__pycache__", ".pytest_cache", ".DS_Store"}
_MANIFEST_IGNORE_SUFFIXES = {".pyc", ".pyo"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_under_business(rel: Path, business_dir: str) -> bool:
    biz_parts = tuple(p for p in Path(business_dir).parts if p not in ("", "."))
    if not biz_parts:
        return True
    return tuple(rel.parts[:len(biz_parts)]) == biz_parts


def _locked_manifest(repo: Path, business_dir: str) -> Dict[str, str]:
    """记录自证仓库中业务目录以外的内容，防止手动 Agent 改测试/配置。"""
    manifest: Dict[str, str] = {}
    for path in sorted(repo.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(repo)
        if _is_under_business(rel, business_dir):
            continue
        if any(part in _MANIFEST_IGNORE_NAMES for part in rel.parts):
            continue
        if path.suffix in _MANIFEST_IGNORE_SUFFIXES:
            continue
        key = rel.as_posix()
        if path.is_symlink():
            manifest[key] = "link:" + os.readlink(path)
        elif path.is_dir():
            manifest[key] = "dir"
        elif path.is_file():
            manifest[key] = "file:" + _sha256_file(path)
    return manifest


def _write_manifest(path: Path, manifest: Dict[str, str]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def _read_manifest(path: Path) -> Dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("自证锁定清单格式错误")
    return {str(k): str(v) for k, v in data.items()}


def _proof_root_is_safe(path: Path) -> bool:
    try:
        root = (BASE_DIR / "tmp").resolve()
        resolved = path.resolve()
        return resolved != root and str(resolved).startswith(str(root) + os.sep)
    except Exception:
        return False


def _cleanup_proof(proof: Optional[Dict]) -> None:
    if not isinstance(proof, dict) or not proof.get("root"):
        return
    root = Path(str(proof["root"]))
    if _proof_root_is_safe(root):
        shutil.rmtree(root, ignore_errors=True)


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
    index_path = _backup_index_path()
    index_tmp = index_path.with_suffix(index_path.suffix + ".tmp")
    try:
        with open(index_tmp, "w", encoding="utf-8") as f:
            json.dump({"backups": index}, f, ensure_ascii=False, indent=2)
        os.replace(index_tmp, index_path)
    except Exception as e:
        try:
            index_tmp.unlink()
        except Exception:
            pass
        shutil.rmtree(dst, ignore_errors=True)
        log_event("backup", "备份索引写入失败，已撤销未登记备份: %s" % e, result="WARN")
        return None
    log_event("backup", "已备份 arena_repo -> backups/%s (tag=%s)" % (bid, tag))
    return bid


def latest_backup() -> Optional[Dict]:
    """返回最近一次可用备份的登记信息（目录必须仍存在）。"""
    for entry in reversed(_load_backup_index()):
        if entry.get("path") and Path(entry["path"]).exists():
            return entry
    return None


def _discard_pending_proof(message: Optional[str] = None) -> bool:
    state = load_state()
    proof = state.get("pending_proof")
    if not proof:
        return False
    _cleanup_proof(proof)
    state["pending_proof"] = None
    if message:
        _set_msg(state, message)
    save_state(state)
    return True


def _restore_arena_unlocked() -> Dict:
    """从 backups/ 最近一次备份还原 arena_repo（规范【6】）。

    - 有备份：清空 arena 当前内容（含未跟踪临时文件），再完整复制备份回去
      （历史测试包含在备份内，因此"保留历史测试"天然满足）；
    - 无备份：git 兜底（checkout 已跟踪文件 + clean 未跟踪，但排除 tests/，
      避免误删人类手放、尚未 commit 的初始历史测试）；
    - 两者都不可用：返回失败（不抛异常）。
    """
    cfg = load_config()
    if cfg.get("mock"):
        return {"ok": True, "mock": True,
                "message": "MOCK 演练模式：未读写 arena_repo，无需还原"}
    arena = arena_path()
    if not arena.exists():
        return {"ok": False, "error": "arena_repo 不存在，无法还原"}
    if not repo_ops.is_arena_ready():
        return {"ok": False, "error": "arena_repo 未就绪（不是可用的 git 仓库或路径非法）"}

    entry = latest_backup()
    if entry is not None:
        try:
            repo_ops.clean_dir_contents(arena)
            shutil.copytree(Path(entry["path"]), arena, dirs_exist_ok=True)
            cancelled = _discard_pending_proof("arena 已还原；原手动自证目录已取消，请重新运行全红")
            log_event("restore", "已从备份 %s 还原 arena_repo" % entry["id"])
            suffix = "；已取消待处理的手动自证" if cancelled else ""
            return {"ok": True, "backup_id": entry["id"],
                    "message": "已还原到备份 %s%s" % (entry["id"], suffix)}
        except Exception as e:
            log_event("restore", "从备份还原失败: %s" % e, result="WARN")
            return {"ok": False, "error": "还原失败: %s" % e}

    # git 兜底
    tdir = cfg.get("history_tests_dir", "tests")
    try:
        repo_ops.git(arena, "checkout", "--", ".")
        repo_ops.git(arena, "clean", "-fdx", "-e", tdir + "/", "-e", tdir)
        cancelled = _discard_pending_proof("arena 已还原；原手动自证目录已取消，请重新运行全红")
        log_event("restore", "无备份，已用 git 还原（保留 %s/ 下未跟踪文件）" % tdir)
        suffix = "；已取消待处理的手动自证" if cancelled else ""
        return {"ok": True, "backup_id": None,
                "message": "无备份，已用 git 还原（保留 %s/）%s" % (tdir, suffix)}
    except Exception as e:
        return {"ok": False, "error": "git 还原失败: %s" % e}


def restore_arena() -> Dict:
    """串行执行 arena 还原；若正在等待手动自证，同时取消该过期工作区。"""
    with VERIFY_LOCK:
        return _restore_arena_unlocked()


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
    state = load_state()
    if state.get("phase") != "answering":
        return {"ok": False, "error": "当前不是答题阶段（phase=%s）" % state.get("phase")}
    if state.get("pending_proof"):
        return {"ok": False, "error": "正在等待手动自证，不能导入答题材料"}

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


def lint_hidden_test(py_path: str | Path, required_count: Optional[int] = None) -> Dict:
    """隐藏测试静态闸门（出题规范【README「测试规范」/ arena_repo TESTING_GUIDELINES.md】的机器校验）。

    阻断级（任一命中即拒绝导入，不进入全红/手动自证）：
    1. 语法错误（ast 解析失败）；
    2. 不是恰好 required_count 个模块顶层、无参数的普通 test_* 函数；
    3. 依赖 tests/ 内其他模块：import tests / from tests ...；
    4. 相对导入（from . / from ..）：验题时单文件拷贝到临时仓库，相对导入必挂；
    5. fixture、参数化、skip/xfail、嵌套/类/async 测试或缺少真断言；
    6. 测试数量不符「一题一缺陷、三测同源」：必须恰好 required_count 个 test_*
       函数（config.proposal_test_count，默认 3），全部针对同一缺陷。

    警告级（只写日志提示，不阻断）：
    - 测试引用了多个业务模块（src/...）—— 疑似一道题里测了多个不相关问题
      （"同一问题"无法静态确证，仅启发式提示）。
    """
    path = Path(py_path)
    errors: List[str] = []
    warnings: List[str] = []
    cfg = load_config()
    if required_count is None:
        try:
            required_count = int(cfg.get("proposal_test_count", 3))
        except (TypeError, ValueError):
            required_count = 3
    business_dir = str(cfg.get("business_dir", "src")).strip("/") or "src"
    try:
        source = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "errors": ["测试文件读取失败: %s" % e], "warnings": []}
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"ok": False, "errors": ["语法错误（第 %d 行）: %s" % (e.lineno or 0, e.msg)],
                "warnings": []}

    fixture_names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                d = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                if "fixture" in d:
                    fixture_names.add(node.name)
                    errors.append("禁止定义 @pytest.fixture：隐藏测试必须在 test_* 内自行构造数据")

    top_tests = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    all_tests = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    if len(all_tests) != len(top_tests):
        errors.append("所有 test_* 必须是模块顶层普通函数；禁止嵌套测试、测试类方法或 async 测试")

    test_count = len(top_tests)
    business_modules: set = set()
    for node in ast.walk(tree):
        # import 检查（顺带收集业务模块引用，供"三测同源"启发式）
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tests" or alias.name.startswith("tests."):
                    errors.append("禁止依赖 tests/ 历史测试模块: import %s" % alias.name)
                if alias.name == business_dir or alias.name.startswith(business_dir + "."):
                    business_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                errors.append("禁止相对导入（验题为单文件拷贝，from . / from .. 必失败）")
            elif mod == "tests" or mod.startswith("tests."):
                errors.append("禁止依赖 tests/ 历史测试模块: from %s import ..." % mod)
            if mod == business_dir or mod.startswith(business_dir + "."):
                business_modules.add(mod)

    for node in top_tests:
        has_assert = any(
            isinstance(k, ast.Assert) or
            (isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute)
             and isinstance(k.func.value, ast.Name) and k.func.value.id == "pytest"
             and k.func.attr == "raises")
            for k in ast.walk(node))
        if not has_assert:
            errors.append("%s 内必须包含 assert 或 pytest.raises 真断言" % node.name)

        args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        if args or node.args.vararg is not None or node.args.kwarg is not None:
            errors.append("%s 禁止使用 fixture/参数注入；测试函数必须无参数" % node.name)

        for dec in node.decorator_list:
            d = ast.unparse(dec) if hasattr(ast, "unparse") else ""
            if "parametrize" in d:
                errors.append("%s 禁止参数化；三个顶层函数必须对应三条实际测试" % node.name)
            if any(mark in d for mark in ("skip", "xfail")):
                errors.append("%s 禁止 skip/skipif/xfail；测试必须真实 RED/GREEN" % node.name)

        for k in ast.walk(node):
            if isinstance(k, ast.Call) and isinstance(k.func, ast.Attribute):
                owner = k.func.value.id if isinstance(k.func.value, ast.Name) else ""
                if owner == "pytest" and k.func.attr in ("skip", "importorskip", "xfail"):
                    errors.append("%s 禁止调用 pytest.%s()" % (node.name, k.func.attr))
            if isinstance(k, ast.Call) and isinstance(k.func, ast.Name) and k.func.id in fixture_names:
                errors.append("%s 禁止调用 fixture %s()" % (node.name, k.func.id))
    if test_count == 0:
        errors.append("文件中没有任何 test_* 函数（pytest 收集不到测试，全绿判定必 FAIL）")
    elif test_count != required_count:
        errors.append("「一题一缺陷、三测同源」：hidden_tests.py 必须恰好包含 %d 个 test_* 函数"
                      "（当前 %d 个），且全部针对同一个缺陷" % (required_count, test_count))
    if len(business_modules) > 1:
        warnings.append("测试引用了多个业务模块（%s）——本赛制要求每次出题只引入一个缺陷，"
                        "三个用例测同一问题；请确认不是把多个不相关问题塞进了同一道题"
                        % ", ".join(sorted(business_modules)))
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "test_count": test_count}


def read_current_prompt() -> Dict:
    """读取当前需求（next_prompt.md）全文，供 Web/CLI 展示与复制。

    返回 {ok, name, content, message}。MOCK 演练模式下 current_prompt_file
    为 "mock://round_N" 标记，返回占位文本（演练不产生真实需求文件）。
    """
    cfg = load_config()
    state = load_state()
    proof = state.get("pending_proof") or {}
    if proof.get("stage") == "awaiting_manual_proof" and proof.get("prompt"):
        prompt_path = Path(str(proof["prompt"]))
        if prompt_path.is_file():
            return {"ok": True, "name": "next_prompt.md（手动自证）",
                    "content": prompt_path.read_text(encoding="utf-8"),
                    "message": ""}
    name = state.get("current_prompt_file")
    if not name:
        return {"ok": True, "name": None, "content": None, "message": "等待首轮需求"}
    if isinstance(name, str) and name.startswith(MOCK_PROMPT_PREFIX):
        return {"ok": True, "name": name,
                "content": "（MOCK 演练）这是演练占位需求——真实模式下，此处显示当前选手"
                           "要实现的 next_prompt.md 全文。",
                "message": ""}
    p = resolve_path(cfg.get("prompts_dir", "prompts")) / name
    if not p.exists():
        return {"ok": True, "name": name, "content": None, "message": "需求文件缺失: %s" % name}
    return {"ok": True, "name": name, "content": p.read_text(encoding="utf-8"), "message": ""}


def set_initial_prompt(prompt_file: str) -> Dict:
    """设置首轮需求（第一位选手作答的提示词，由人类主办方提供，一次性）。

    仅在 current_prompt_file 为空（或为 MOCK 演练标记）时允许设置；
    复制到 prompts/round_1_initial.md 并指向它。后续每轮需求由选手出题自动产生。
    """
    pf = Path(prompt_file)
    if not pf.exists():
        return {"ok": False, "error": "需求文档不存在"}
    try:
        text = pf.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": "需求文档读取失败: %s" % e}
    if not text.strip():
        return {"ok": False, "error": "需求文档为空"}

    state = load_state()
    cur = state.get("current_prompt_file")
    if cur and not str(cur).startswith(MOCK_PROMPT_PREFIX):
        return {"ok": False, "error": "当前已有需求（%s）。首轮需求只在比赛开局、"
                                      "尚无任何需求时设置；之后每轮需求由选手出题产生" % cur}

    cfg = load_config()
    prompts_dir = resolve_path(cfg.get("prompts_dir", "prompts"))
    prompts_dir.mkdir(parents=True, exist_ok=True)
    name = "round_1_initial.md"
    (prompts_dir / name).write_text(text, encoding="utf-8")

    state["current_prompt_file"] = name
    _set_msg(state, "首轮需求已导入，请交给第一位选手作答")
    save_state(state)
    log_event("set-first-prompt", "导入首轮需求（%s）" % name,
              player=state.get("current_player"), round_=state.get("round"))
    return {"ok": True, "name": name, "message": "首轮需求已导入，请交给第一位选手作答"}


def import_proposal(prompt_file: str, test_file: str) -> Dict:
    """导入出题材料（等同 POST /api/proposal）：next_prompt.md + hidden_tests.py。

    先过 lint_hidden_test 静态闸门（出题规范），不合规直接拒绝导入；合规后
    第一次推进运行全红，第二次推进运行手动自证后的全绿。"""
    state = load_state()
    if state.get("phase") != "proposing":
        return {"ok": False, "error": "当前不是出题阶段（phase=%s）" % state.get("phase")}
    if (state.get("pending_proof") or {}).get("stage") == "awaiting_manual_proof":
        return {"ok": False, "error": "全红已通过，正在等待手动自证；不能替换本轮出题材料"}

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

    lint = lint_hidden_test(tf)
    if not lint["ok"]:
        return {"ok": False, "error": "隐藏测试不合规，已拒绝导入：" +
                "；".join(lint["errors"]) +
                "（规范见 arena_repo/TESTING_GUIDELINES.md 或 README「测试规范」："
                "一题一缺陷、恰好 3 个测试同源、单文件自包含、数据内联）"}
    if lint["warnings"]:
        log_event("lint-warn", "隐藏测试警告：" + "；".join(lint["warnings"]))

    stage = UPLOADS_DIR / ("proposal_%s" % uuid.uuid4().hex[:8])
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pf, stage / "next_prompt.md")
    shutil.copy2(tf, stage / "hidden_tests.py")

    state = load_state()
    state["pending_proposal"] = {"prompt": str(stage / "next_prompt.md"),
                                 "test": str(stage / "hidden_tests.py")}
    _set_msg(state, "已导入出题材料，等待全红判定")
    save_state(state)
    log_event("import-proposal", "导入出题材料（需求文档 + 隐藏测试）",
              player=state.get("current_player"), round_=state.get("round"))
    return {"ok": True, "pending": state["pending_proposal"],
            "message": "已导入出题材料（需求 + 隐藏测试），等待全红判定"
                       + ("（警告：%s）" % "；".join(lint["warnings"]) if lint["warnings"] else "")}


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
# MOCK 演练模式（config.json "mock": true，可经 /api/mock 或 Web 按钮切换）
#
# 用途：人类上真实流程前先走一遍完整操作。每次判定随机出 PASS/FAIL，
# 状态推进（phase/round/淘汰/积分/日志）与真实流程完全一致，但：
# - 不运行 pytest；不读写 arena_repo；不需要任何材料。
# 日志与返回值均带 MOCK 标记，避免与真实结果混淆。
# ---------------------------------------------------------------------------

# 随机通过率（演练节奏：略偏 PASS，让流程往前走，FAIL 也会自然出现）
MOCK_ANSWER_PASS_RATE = 0.65
MOCK_PROPOSAL_PASS_RATE = 0.65

# mock 需求标记：current_prompt_file 用此前缀时 /api/prompt 返回占位文本
MOCK_PROMPT_PREFIX = "mock://"


def is_mock_enabled(cfg: Optional[Dict] = None) -> bool:
    cfg = cfg or load_config()
    return bool(cfg.get("mock"))


def _mock_counts(result: str) -> Dict:
    """生成一套演练用的两行测试计数（仅计数，不含任何测试内容）。

    PASS -> 全绿；FAIL -> 历史保持全绿、隐藏测试挂掉一部分（典型失败形态）。
    """
    cfg = load_config()
    try:
        hidden_total = int(cfg.get("proposal_test_count", 3))
    except (TypeError, ValueError):
        hidden_total = 3
    hist_total = random.randint(5, 40)
    if result == "PASS":
        hp, dp = hist_total, hidden_total
    else:
        hp = hist_total
        dp = random.randint(0, max(0, hidden_total - 1))
    return {
        "history": {"passed": hp, "total": hist_total, "ok": hp >= hist_total},
        "hidden": {"passed": dp, "total": hidden_total, "ok": dp >= hidden_total},
        "overall": result,
    }


def _mock_verify_answer() -> Dict:
    """MOCK 演练版答题验收：随机判定，状态推进与真实流程一致。"""
    time.sleep(1.0)  # 模拟真实评测的运行延迟（约 1 秒），让录屏有"在跑"的观感
    with VERIFY_LOCK:
        state = load_state()
        if state.get("status") == "finished":
            return {"ok": False, "result": None, "reason": "比赛已结束"}
        if state.get("phase") != "answering":
            return {"ok": False, "result": None,
                    "reason": "当前不是答题阶段（phase=%s）" % state.get("phase")}
        player = state.get("current_player")
        rnd = state.get("round", 1)
        result = "PASS" if random.random() < MOCK_ANSWER_PASS_RATE else "FAIL"
        counts = _mock_counts(result)

        state2 = load_state()
        state2["last_result"] = result
        state2["last_test_summary"] = counts
        state2["pending_answer"] = None
        if result == "PASS":
            state2["phase"] = "proposing"  # 轮次不变，等待该选手出题
            state2["scores"] = dict(state2.get("scores", {}))
            state2["scores"][player] = state2["scores"].get(player, 0) + 1
            _set_msg(state2, "[MOCK] 选手 %s 答题 PASS，请提交出题材料" % player)
            message = "[MOCK] 选手 %s 答题通过，请继续出题" % player
        else:
            _advance(state2, eliminate_current=True)
            state2["phase"] = "answering"
            _set_msg(state2, "[MOCK] 选手 %s 答题 FAIL，已淘汰并切换" % player)
            message = "[MOCK] 选手 %s 答题失败，已切换到 %s" % (player, state2.get("current_player"))
        save_state(state2)
        log_event("judge-answer",
                  "MOCK 演练：选手 %s 答题 %s（随机模拟，未运行 pytest、未读写 arena_repo）" % (player, result),
                  result=result, player=player, round_=rnd)
        return {"ok": True, "result": result, "player": player, "round": rnd,
                "mock": True, "eliminated": result == "FAIL",
                "history": counts["history"], "hidden": counts["hidden"],
                "next_player": state2.get("current_player"),
                "message": message}


def _mock_verify_proposal() -> Dict:
    """MOCK 演练版手动自证：第一次模拟全红，第二次模拟全绿。"""
    time.sleep(1.0)
    with VERIFY_LOCK:
        state = load_state()
        if state.get("status") == "finished":
            return {"ok": False, "result": None, "reason": "比赛已结束"}
        if state.get("phase") != "proposing":
            return {"ok": False, "result": None,
                    "reason": "当前选手尚未通过答题验收（phase=%s）" % state.get("phase")}
        player = state.get("current_player")
        rnd = state.get("round", 1)
        proof = state.get("pending_proof") or {}

        # 第一次推进：模拟新测试全红，成功后持久化等待人工自证。
        if proof.get("stage") != "awaiting_manual_proof":
            result = "PASS" if random.random() < MOCK_PROPOSAL_PASS_RATE else "FAIL"
            hidden_total = _proposal_test_count()
            history_total = random.randint(5, 40)
            counts = {
                "history": {"passed": history_total, "total": history_total, "ok": True},
                "hidden": {"passed": 0, "total": hidden_total, "ok": False},
                "overall": result,
            }
            state2 = load_state()
            state2["last_result"] = result
            state2["last_test_summary"] = counts
            if result == "PASS":
                state2["pending_proof"] = {
                    "stage": "awaiting_manual_proof", "mock": True,
                    "repo": "mock://manual-proof/round-%s/%s" % (rnd, player),
                    "prompt": "mock://next_prompt.md", "player": player, "round": rnd,
                }
                _set_msg(state2, "[MOCK] 新测试全红通过；请手动演练自证，完成后再次点击 NEXT")
                message = "[MOCK] 全红通过，等待手动自证；完成后再次点击 NEXT"
                save_state(state2)
                log_event("proposal-red", "MOCK 演练：新测试全部 RED，进入手动自证等待",
                          result="PASS", player=player, round_=rnd)
                return {"ok": True, "result": "PASS", "gate": "RED", "mock": True,
                        "player": player, "round": rnd, "eliminated": False,
                        "history": counts["history"], "hidden": counts["hidden"],
                        "proof_repo": state2["pending_proof"]["repo"], "message": message}

            _advance(state2, eliminate_current=True)
            state2["phase"] = "answering"
            state2["pending_proposal"] = None
            state2["pending_proof"] = None
            _set_msg(state2, "[MOCK] 新测试未全部 RED，出题失败并切换")
            save_state(state2)
            log_event("proposal-red", "MOCK 演练：全红判定 FAIL",
                      result="FAIL", player=player, round_=rnd)
            return {"ok": True, "result": "FAIL", "gate": "RED", "mock": True,
                    "player": player, "round": rnd, "eliminated": True,
                    "history": counts["history"], "hidden": counts["hidden"],
                    "next_player": state2.get("current_player"),
                    "message": "[MOCK] 新测试未全部 RED，已淘汰并切换"}

        # 第二次推进：人类完成手动自证后，模拟最终全绿。
        result = "PASS" if random.random() < MOCK_PROPOSAL_PASS_RATE else "FAIL"
        counts = _mock_counts(result)

        state2 = load_state()
        state2["last_result"] = result
        state2["last_test_summary"] = counts
        state2["pending_proposal"] = None
        state2["pending_proof"] = None
        if result == "PASS":
            new_round = rnd + 1
            state2["current_prompt_file"] = "%sround_%d" % (MOCK_PROMPT_PREFIX, new_round)
            state2["round"] = new_round
            _advance(state2, eliminate_current=False)  # 交棒：出题者保留，切换下一位
            state2["phase"] = "answering"
            state2["scores"] = dict(state2.get("scores", {}))
            state2["scores"][player] = state2["scores"].get(player, 0) + 1
            _set_msg(state2, "[MOCK] 手动自证全绿，选手 %s 出题合法，已交棒（第 %d 轮）" % (player, new_round))
            message = "[MOCK] 出题合法，已交棒：第 %d 轮，当前选手 %s" % (
                new_round, state2.get("current_player"))
        else:
            _advance(state2, eliminate_current=True)
            state2["phase"] = "answering"
            _set_msg(state2, "[MOCK] 手动自证未全绿，选手 %s 已淘汰并切换" % player)
            message = "[MOCK] 选手 %s 出题失败，已切换到 %s" % (player, state2.get("current_player"))
        save_state(state2)
        log_event("proposal-green",
                  "MOCK 演练：手动自证全绿判定 %s（未运行 pytest、未读写 arena_repo）" % result,
                  result=result, player=player, round_=rnd)
        return {"ok": True, "result": result, "player": player, "round": rnd,
                "gate": "GREEN", "mock": True, "eliminated": result == "FAIL",
                "new_round": state2.get("round"),
                "history": counts["history"], "hidden": counts["hidden"],
                "next_player": state2.get("current_player"),
                "message": message}


# ---------------------------------------------------------------------------
# 答题验收（不调用任何模型）
# ---------------------------------------------------------------------------

def verify_answer() -> Dict:
    """验收当前选手的答题（规范【6】）。

    流程：前置检查 -> 备份当前 -> tests 内容清单(前) -> 应用业务文件 ->
    tests 内容清单(后) 对比（篡改即 FAIL 并回滚）-> 把本轮隐藏测试拷入 tests/ ->
    跑 pytest（历史+隐藏）-> 全绿则隐藏测试转正+再备份+phase=proposing；
    否则回滚+淘汰+切换下一位。全程不调用任何 AI。

    MOCK 演练模式（config.json "mock": true）时走 _mock_verify_answer：
    随机判定、状态推进一致，但不跑 pytest、不碰 arena_repo、不需要材料。
    """
    cfg = load_config()
    if cfg.get("mock"):
        return _mock_verify_answer()
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
        pre_backup = backup_arena("pre_answer_r%s_%s" % (rnd, player))
        if pre_backup is None:
            return {"ok": False, "result": None,
                    "reason": "基础设施错误：答题前备份失败；未应用材料，也不淘汰选手"}

        def _fail(reason: str, eliminate: bool = True, res: Optional[Dict] = None) -> Dict:
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
            state2["pending_proof"] = None
            state2["last_test_summary"] = _summary_counts(res, overall="FAIL") if res is not None else None
            _set_msg(state2, "选手 %s 答题失败：%s" % (player, reason))
            save_state(state2)
            return {"ok": True, "result": "FAIL", "reason": reason, "player": player,
                    "round": rnd, "eliminated": eliminate,
                    "next_player": state2.get("current_player"),
                    "message": "选手 %s 答题失败（%s），已回滚并切换" % (player, reason)}

        # ---- 2. 应用前 tests 内容哈希清单 ----
        before = repo_ops.tests_status_map()

        # ---- 3. 应用业务文件（只进 business_dir，严禁触碰 tests/） ----
        applied = apply_business_files(pending)
        if not applied.get("ok"):
            return _fail("答题材料无效：%s" % applied.get("error", "应用失败"))
        if applied.get("warning"):
            log_event("apply", applied["warning"], result="WARN", player=player, round_=rnd)

        # ---- 4. 应用后 tests 内容清单对比（篡改历史测试 -> 立即回滚并判 FAIL） ----
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

        # 业务代码在 pytest 导入/执行期间也不得改写历史测试；临时隐藏文件单独排除。
        post_tests = repo_ops.tests_status_map(ignore_names={hidden_name})
        if post_tests != after:
            hidden_dest = res.get("hidden_dest")
            if hidden_dest and Path(hidden_dest).exists():
                try:
                    Path(hidden_dest).unlink()
                except Exception:
                    pass
            return _fail("检测到 pytest 运行期间历史测试内容发生变化", res=res)

        green_ok, green_reason = _validate_all_green(res, _proposal_test_count(cfg))
        if not green_ok:
            hidden_dest = res.get("hidden_dest")
            if hidden_dest and Path(hidden_dest).exists():
                try:
                    Path(hidden_dest).unlink()
                except Exception:
                    pass
            return _fail(green_reason, res=res)

        # ---- 6. 全绿：隐藏测试永久转正 + 成功点备份 + 等待该选手出题 ----
        hidden_dest = Path(res["hidden_dest"]) if res.get("hidden_dest") else None
        if not hidden_dest or not hidden_dest.exists():
            return _fail("本轮测试虽已运行，但测试文件在转正前丢失", eliminate=False, res=res)
        final_name = "test_round_%s_%s.py" % (rnd, player)
        final_path = tests_dir / final_name
        if final_path.exists():
            final_path = tests_dir / ("test_round_%s_%s_%s.py" % (rnd, player, uuid.uuid4().hex[:6]))
        shutil.move(str(hidden_dest), str(final_path))

        success_backup = backup_arena("answer_ok_r%s_%s" % (rnd, player))
        if success_backup is None:
            restore_arena()  # 最近备份就是本轮 pre-answer；恢复后允许原选手重试
            state2 = load_state()
            state2["last_result"] = None
            state2["last_test_summary"] = None
            _set_msg(state2, "基础设施错误：答题通过后备份失败，已恢复到判定前；选手未淘汰")
            save_state(state2)
            return {"ok": False, "result": None,
                    "reason": "基础设施错误：成功点备份失败，已恢复到判定前；可重新验收"}

        # 成功点安全落盘后，才消费 Harness 暂存的本轮隐藏测试。
        for extra in hidden_dir.glob("*.py"):
            if extra.name != "hidden_tests.py":
                extra.unlink()
        try:
            hidden_src.unlink()
        except Exception:
            pass

        counts = _summary_counts(res)
        state2 = load_state()
        state2["phase"] = "proposing"  # 轮次不变，等待该选手出题
        state2["scores"] = dict(state2.get("scores", {}))
        state2["scores"][player] = state2["scores"].get(player, 0) + 1
        state2["last_result"] = "PASS"
        state2["last_test_summary"] = counts
        state2["pending_answer"] = None
        state2["pending_proof"] = None
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
# 出题：全红 -> 人工自证 -> 全绿 -> 交棒
# ---------------------------------------------------------------------------

def verify_proposal() -> Dict:
    """两次推进完成出题校验；Harness 永不启动或调用 AI Agent。

    第一次：在一次性副本中运行历史 + 新测试，要求历史全绿、新测试全部 RED；
    通过后创建持久手动自证目录（不含新测试），返回目录与提示词路径并暂停。

    第二次：人类已在该目录手动启动同模型 OpenCode（或真人编码）后再次调用；
    Harness 校验仅业务目录发生变化，再复制工作区、注入同一份新测试，要求全部 GREEN。
    自证代码随后丢弃，仅保存提示词和隐藏测试并交棒。
    """
    cfg = load_config()
    if cfg.get("mock"):
        return _mock_verify_proposal()

    with VERIFY_LOCK:
        state = load_state()
        player = state.get("current_player")
        rnd = state.get("round", 1)

        if state.get("status") == "finished":
            return {"ok": False, "result": None, "reason": "比赛已结束"}
        if not repo_ops.is_arena_ready():
            _set_msg(state, "arena_repo 未就绪，无法校验出题")
            save_state(state)
            return {"ok": False, "result": None,
                    "reason": "arena_repo 未就绪（不存在或不是独立可用的 git 仓库）"}
        if state.get("phase") != "proposing":
            return {"ok": False, "result": None,
                    "reason": "当前选手尚未通过答题验收（phase=%s）" % state.get("phase")}

        proof = state.get("pending_proof") or {}
        pending = state.get("pending_proposal") or {}
        prompt_path = Path(str(pending.get("prompt", ""))) if pending.get("prompt") else None
        test_path = Path(str(pending.get("test", ""))) if pending.get("test") else None

        if proof.get("stage") != "awaiting_manual_proof" and not (
                prompt_path and test_path and prompt_path.exists() and test_path.exists()):
            picked = pickup_proposal()
            if picked.get("ok"):
                state = load_state()
                pending = state.get("pending_proposal") or {}
                prompt_path = Path(str(pending.get("prompt", ""))) if pending.get("prompt") else None
                test_path = Path(str(pending.get("test", ""))) if pending.get("test") else None
            if not (prompt_path and test_path and prompt_path.exists() and test_path.exists()):
                return {"ok": False, "result": None,
                        "reason": "尚未导入出题材料：上传 next_prompt.md + hidden_tests.py，"
                                  "或将它们放入 inbox/ 后重试"}

        def _fail(reason: str, gate: str, res: Optional[Dict] = None) -> Dict:
            current = load_state()
            _cleanup_proof(current.get("pending_proof"))
            _advance(current, eliminate_current=True)
            current["last_result"] = "FAIL"
            current["phase"] = "answering"
            current["pending_proposal"] = None
            current["pending_proof"] = None
            current["last_test_summary"] = _summary_counts(res, overall="FAIL") if res is not None else None
            _set_msg(current, "选手 %s 出题失败（%s）：%s" % (player, gate, reason))
            save_state(current)
            log_event("proposal-%s" % gate.lower(), "选手 %s 出题 FAIL：%s" % (player, reason),
                      result="FAIL", player=player, round_=rnd)
            return {"ok": True, "result": "FAIL", "gate": gate, "reason": reason,
                    "player": player, "round": rnd, "eliminated": True,
                    "next_player": current.get("current_player"),
                    "history": (_summary_counts(res, overall="FAIL")["history"] if res else None),
                    "hidden": (_summary_counts(res, overall="FAIL")["hidden"] if res else None),
                    "message": "选手 %s 出题失败（%s），正式 arena 未改动，已切换" % (player, reason)}

        def _infra(reason: str, keep_proof: bool = False) -> Dict:
            current = load_state()
            if not keep_proof:
                _cleanup_proof(current.get("pending_proof"))
                current["pending_proof"] = None
            _set_msg(current, "基础设施错误：%s；选手未淘汰" % reason)
            save_state(current)
            log_event("proposal-infra", reason, result="WARN", player=player, round_=rnd)
            return {"ok": False, "result": None, "reason": "基础设施错误：%s" % reason}

        expected = _proposal_test_count(cfg)

        # ------------------------------------------------------------------
        # 第一次推进：新测试必须全部 RED
        # ------------------------------------------------------------------
        if proof.get("stage") != "awaiting_manual_proof":
            assert prompt_path is not None and test_path is not None
            try:
                prompt_text = prompt_path.read_text(encoding="utf-8")
            except Exception as e:
                return _infra("需求文档读取失败: %s" % e)
            if not prompt_text.strip():
                return _fail("需求文档为空", "RED")

            lint = lint_hidden_test(test_path, required_count=expected)
            if not lint.get("ok"):
                return _fail("隐藏测试不合规：%s" % "；".join(lint.get("errors", [])), "RED")

            root = BASE_DIR / "tmp" / ("manual_proof_r%s_%s_%s" % (
                rnd, player, uuid.uuid4().hex[:10]))
            red_repo = root / "red_check"
            try:
                root.mkdir(parents=True, exist_ok=False)
                repo_ops.copy_arena_to(red_repo, for_verify=True)
            except Exception as e:
                shutil.rmtree(root, ignore_errors=True)
                return _infra("创建全红检查副本失败: %s" % e)

            hidden_name = "test_proposal_red_%s.py" % uuid.uuid4().hex[:8]
            try:
                res = run_pytest(red_repo, extra_test_file=test_path, hidden_name=hidden_name)
            finally:
                shutil.rmtree(red_repo, ignore_errors=True)
            if res.get("log_text"):
                print("[pytest proposal RED r%s %s] exit=%s\n%s" % (
                    rnd, player, res.get("exit_code"), res.get("log_text", "")[-4000:]), file=sys.stderr)
            if int(res.get("exit_code", -1)) < 0:
                shutil.rmtree(root, ignore_errors=True)
                return _infra("全红 pytest 无法完成或超时")
            red_ok, red_reason = _validate_all_red(res, expected)
            if not red_ok:
                shutil.rmtree(root, ignore_errors=True)
                return _fail(red_reason, "RED", res)

            proof_repo = root / "repo"
            prompt_copy = root / "next_prompt.md"
            manifest_path = root / "locked_manifest.json"
            try:
                # 自证目录是当前正式环境的干净副本，不含新隐藏测试。
                repo_ops.copy_arena_to(proof_repo, for_verify=False)
                shutil.copy2(prompt_path, prompt_copy)
                manifest = _locked_manifest(proof_repo, str(cfg.get("business_dir", "src")))
                _write_manifest(manifest_path, manifest)
            except Exception as e:
                shutil.rmtree(root, ignore_errors=True)
                return _infra("创建手动自证目录失败: %s" % e)

            record = {
                "stage": "awaiting_manual_proof",
                "root": str(root),
                "repo": str(proof_repo),
                "prompt": str(prompt_copy),
                "manifest": str(manifest_path),
                "player": player,
                "round": rnd,
                "prompt_sha256": _sha256_file(prompt_path),
                "test_sha256": _sha256_file(test_path),
            }
            counts = _summary_counts(res, overall="PASS")
            current = load_state()
            current["pending_proof"] = record
            current["last_result"] = "PASS"
            current["last_test_summary"] = counts
            _set_msg(current, "全红通过；请在 %s 手动打开同模型 OpenCode，完成后再次点击 NEXT" % proof_repo)
            save_state(current)
            log_event("proposal-red", "新测试全部 RED；等待手动自证，目录 %s" % proof_repo,
                      result="PASS", player=player, round_=rnd)
            return {"ok": True, "result": "PASS", "gate": "RED", "player": player,
                    "round": rnd, "eliminated": False,
                    "history": counts["history"], "hidden": counts["hidden"],
                    "proof_repo": str(proof_repo), "proof_prompt": str(prompt_copy),
                    "message": "全红验证通过；请手动完成自证，完成后再次点击 NEXT"}

        # ------------------------------------------------------------------
        # 第二次推进：人类已完成 OpenCode/真人自证，必须全部 GREEN
        # ------------------------------------------------------------------
        if proof.get("player") != player or int(proof.get("round", -1)) != int(rnd):
            return _infra("手动自证状态与当前选手/轮次不一致")
        if not (prompt_path and test_path and prompt_path.exists() and test_path.exists()):
            return _infra("已暂存的出题材料丢失", keep_proof=True)

        root = Path(str(proof.get("root", "")))
        proof_repo = Path(str(proof.get("repo", "")))
        manifest_path = Path(str(proof.get("manifest", "")))
        if not (_proof_root_is_safe(root) and proof_repo.is_dir() and manifest_path.is_file()):
            current = load_state()
            _cleanup_proof(current.get("pending_proof"))
            current["pending_proof"] = None
            _set_msg(current, "手动自证目录缺失，已重置；再次点击 NEXT 将重新运行全红")
            save_state(current)
            return {"ok": False, "result": None,
                    "reason": "手动自证目录缺失，已重置到全红验证之前"}

        if (_sha256_file(prompt_path) != proof.get("prompt_sha256")
                or _sha256_file(test_path) != proof.get("test_sha256")):
            return _fail("全红后出题材料发生变化", "GREEN")

        try:
            before = _read_manifest(manifest_path)
            after = _locked_manifest(proof_repo, str(cfg.get("business_dir", "src")))
        except Exception as e:
            return _infra("无法校验自证目录锁定文件: %s" % e, keep_proof=True)
        if before != after:
            changed = sorted(set(before) ^ set(after))
            if not changed:
                changed = sorted(k for k in before if before.get(k) != after.get(k))
            detail = "、".join(changed[:8]) or "内容哈希变化"
            return _fail("自证修改了业务目录之外的锁定文件：%s" % detail, "GREEN")

        green_repo = root / "green_check"
        try:
            shutil.rmtree(green_repo, ignore_errors=True)
            shutil.copytree(
                proof_repo, green_repo,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", ".DS_Store"),
            )
        except Exception as e:
            return _infra("复制手动自证结果失败: %s" % e, keep_proof=True)

        hidden_name = "test_proposal_green_%s.py" % uuid.uuid4().hex[:8]
        try:
            res = run_pytest(green_repo, extra_test_file=test_path, hidden_name=hidden_name)
        finally:
            shutil.rmtree(green_repo, ignore_errors=True)
        if res.get("log_text"):
            print("[pytest proposal GREEN r%s %s] exit=%s\n%s" % (
                rnd, player, res.get("exit_code"), res.get("log_text", "")[-4000:]), file=sys.stderr)
        if int(res.get("exit_code", -1)) < 0:
            return _infra("全绿 pytest 无法完成或超时", keep_proof=True)
        green_ok, green_reason = _validate_all_green(res, expected)
        if not green_ok:
            return _fail(green_reason, "GREEN", res)

        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompts_dir = resolve_path(cfg.get("prompts_dir", "prompts"))
        hidden_dir = resolve_path(cfg.get("hidden_tests_dir", "hidden_tests"))
        prompts_dir.mkdir(parents=True, exist_ok=True)
        hidden_dir.mkdir(parents=True, exist_ok=True)
        new_round = rnd + 1
        prompt_name = "round_%s_by_%s.md" % (new_round, player)
        prompt_dest = prompts_dir / prompt_name
        hidden_dest = hidden_dir / "hidden_tests.py"
        prompt_tmp = prompt_dest.with_suffix(prompt_dest.suffix + ".tmp")
        hidden_tmp = hidden_dest.with_suffix(hidden_dest.suffix + ".tmp")
        try:
            prompt_tmp.write_text(prompt_text, encoding="utf-8")
            shutil.copy2(test_path, hidden_tmp)
            os.replace(prompt_tmp, prompt_dest)
            os.replace(hidden_tmp, hidden_dest)
        except Exception as e:
            for tmp in (prompt_tmp, hidden_tmp):
                try:
                    tmp.unlink()
                except Exception:
                    pass
            return _infra("保存下一轮提示词/隐藏测试失败: %s" % e, keep_proof=True)

        counts = _summary_counts(res)
        current = load_state()
        _cleanup_proof(current.get("pending_proof"))
        current["current_prompt_file"] = prompt_name
        current["round"] = new_round
        _advance(current, eliminate_current=False)
        current["phase"] = "answering"
        current["scores"] = dict(current.get("scores", {}))
        current["scores"][player] = current["scores"].get(player, 0) + 1
        current["last_result"] = "PASS"
        current["last_test_summary"] = counts
        current["pending_proposal"] = None
        current["pending_proof"] = None
        _set_msg(current, "手动自证全绿，选手 %s 出题合法，已交棒至第 %s 轮" % (player, new_round))
        save_state(current)
        log_event("proposal-green", "手动自证全部 GREEN：历史 %s/%s，新测试 %s/%s；交棒至第 %s 轮" % (
            res["history"]["passed"], res["history"]["total"],
            res["hidden"]["passed"], res["hidden"]["total"], new_round),
            result="PASS", player=player, round_=rnd)
        return {"ok": True, "result": "PASS", "gate": "GREEN", "player": player,
                "round": rnd, "new_round": new_round, "eliminated": False,
                "next_player": current.get("current_player"),
                "history": counts["history"], "hidden": counts["hidden"],
                "message": "手动自证全绿，出题有效；已交棒至第 %s 轮" % new_round}
