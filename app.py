"""Bug Relay Web 控制台后端（FastAPI，规范【5】）。

启动：uvicorn app:app --host 127.0.0.1 --port 8080 （或 python -m cli.bugrelay web）

设计要点：
- 任何接口都不把 hidden_tests/ 内容返回给前端；文件树过滤 hidden_tests/；
- 测试结果只返回总结果与计数，绝不返回测试函数名/断言/diff；
- arena_repo 未就绪时所有相关接口返回友好提示，绝不抛异常崩溃；
- 框架只在人类点击按钮的瞬间工作（导入/评测/还原），不监听、不轮询选手。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import judge, repo_ops
from core.utils import (
    BASE_DIR, UPLOADS_DIR, ensure_dirs, load_state, log_event, read_log,
    resolve_path,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时：确保目录/初始 state 存在，并刷新 arena_ready（arena 不存在也能启动）
    ensure_dirs()
    repo_ops.refresh_arena_ready()
    state = load_state()
    if state.get("arena_ready"):
        log_event("startup", "框架启动，arena_repo 就绪")
    else:
        log_event("startup", "框架启动（arena_repo 未就绪，相关操作将不可用）")
    yield


app = FastAPI(title="Bug Relay Harness", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """单页控制台。"""
    html_path = BASE_DIR / "templates" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Bug Relay</h1><p>templates/index.html 缺失</p>", status_code=200)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 只读信息接口
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state():
    """当前赛况（轮次/选手/存活/淘汰/arena_ready/最近结果等）。"""
    try:
        ensure_dirs()
        repo_ops.refresh_arena_ready()
        state = load_state()
        return {"ok": True, "state": state}
    except Exception as e:  # 任何异常都不让前端拿到 500 崩溃
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/tree")
async def api_tree():
    """arena_repo 文件树（只读；过滤 hidden_tests/.git/__pycache__ 等）。"""
    try:
        repo_ops.refresh_arena_ready()
        if not repo_ops.is_arena_ready():
            return {"ok": True, "ready": False, "tree": None, "message": "arena_repo 未就绪"}
        tree = repo_ops.build_tree()
        return {"ok": True, "ready": True, "tree": tree,
                "message": "" if tree is None else ""}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/file")
async def api_file(path: str = Query(..., description="arena_repo 内相对路径")):
    """查看 arena_repo 内文本文件内容（前端渲染行号；hidden_tests 一律拒绝）。"""
    try:
        return repo_ops.read_arena_file(path)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/log")
async def api_log(limit: int = Query(100, ge=1, le=500)):
    """操作日志（最新在上；只含框架操作文字，不含测试内容）。"""
    try:
        items = read_log(limit)
        return {"ok": True, "items": items}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/prompt")
async def api_prompt():
    """当前需求 next_prompt.md 内容（只读，完整展示）。"""
    try:
        from core.utils import load_config
        cfg = load_config()
        state = load_state()
        name = state.get("current_prompt_file")
        if not name:
            return {"ok": True, "name": None, "content": None, "message": "等待首轮需求"}
        p = resolve_path(cfg.get("prompts_dir", "prompts")) / name
        if not p.exists():
            return {"ok": True, "name": name, "content": None, "message": "需求文件缺失: %s" % name}
        return {"ok": True, "name": name, "content": p.read_text(encoding="utf-8"), "message": ""}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


# ---------------------------------------------------------------------------
# 操作接口（人类点击触发）
# ---------------------------------------------------------------------------

async def _save_uploads(files: List[UploadFile], staging: Path) -> int:
    """把上传文件按（清洗后的）相对路径落盘到 staging，返回文件数。"""
    staging.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in files:
        rel = repo_ops.sanitize_rel_name(f.filename or "file")
        if not rel:
            continue
        dest = staging / rel
        # 双保险：目标必须仍在 staging 内（防穿越）
        if not str(dest.resolve()).startswith(str(staging.resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = await f.read()
        dest.write_bytes(data)
        n += 1
    return n


@app.post("/api/answer")
async def api_answer(files: List[UploadFile] = File(...)):
    """导入选手的"业务代码改动"（支持 .zip 或多文件/文件夹上传）。"""
    try:
        ensure_dirs()
        if not files:
            return {"ok": False, "error": "没有收到文件"}
        staging = UPLOADS_DIR / ("answer_%s" % uuid.uuid4().hex[:8])
        # 单个 zip：保持 zip 形态暂存（judge 解压应用）
        if len(files) == 1 and (files[0].filename or "").lower().endswith(".zip"):
            zip_path = staging / "upload.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(await files[0].read())
            result = judge.import_answer(str(zip_path))
        else:
            n = await _save_uploads(files, staging)
            if n == 0:
                shutil.rmtree(staging, ignore_errors=True)
                return {"ok": False, "error": "上传文件名非法（含路径穿越）"}
            result = judge.import_answer(str(staging))
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/judge-answer")
async def api_judge_answer():
    """验收答题：应用上传 -> 跑历史+隐藏 pytest -> 全绿转正，否则回滚淘汰。"""
    try:
        # judge.verify_answer 内部有全局锁与 subprocess 调用，放线程池避免阻塞事件循环
        result = await asyncio.to_thread(judge.verify_answer)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/proposal")
async def api_proposal(
    prompt: UploadFile = File(..., description="next_prompt.md（.md/.txt）"),
    test: UploadFile = File(..., description="hidden_tests.py"),
):
    """导入出题材料：给下一棒的需求文档 + 对应隐藏测试。"""
    try:
        ensure_dirs()
        staging = UPLOADS_DIR / ("proposal_%s" % uuid.uuid4().hex[:8])
        staging.mkdir(parents=True, exist_ok=True)
        p_path = staging / "next_prompt.md"
        t_path = staging / "hidden_tests.py"
        p_path.write_bytes(await prompt.read())
        t_path.write_bytes(await test.read())
        return judge.import_proposal(str(p_path), str(t_path))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/judge-proposal")
async def api_judge_proposal():
    """校验出题并交棒：临时目录 + 验题模型自证（单次调用）+ pytest。"""
    try:
        result = await asyncio.to_thread(judge.verify_proposal)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/restore")
async def api_restore():
    """还原 arena_repo 到最近一次备份（人类手动操作）。"""
    try:
        result = await asyncio.to_thread(judge.restore_arena)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
