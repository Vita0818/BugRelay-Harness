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
    BASE_DIR, UPLOADS_DIR, draw_order, ensure_dirs, load_config, load_state,
    log_event, read_log, resolve_path, set_config_flag, set_player_models,
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
    """单页控制台。HTML 禁缓存，保证改动后刷新即生效（静态资源靠 ?v= 版本号更新）。"""
    html_path = BASE_DIR / "templates" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Bug Relay</h1><p>templates/index.html 缺失</p>", status_code=200)
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 只读信息接口
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def api_state():
    """当前赛况（轮次/选手/存活/淘汰/arena_ready/最近结果等）+ inbox 材料就位情况。

    step 为派生字段（2×2 四格：① 作答 ② 出题 / ③ 判定 ④ 自证）：
    phase=answering -> step 1（等待作答材料）；phase=proposing -> step 2（等待出题材料）。
    步骤 3/4 是按钮触发的瞬时评测动作，由前端在请求进行中本地高亮。
    """
    try:
        ensure_dirs()
        repo_ops.refresh_arena_ready()
        state = load_state()
        step = 2 if state.get("phase") == "proposing" else 1
        return {"ok": True, "state": state, "inbox": judge.inbox_status(), "step": step,
                "mock": judge.is_mock_enabled()}
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
    """当前需求 next_prompt.md 内容（只读，完整展示）。

    MOCK 演练模式下 current_prompt_file 为 "mock://round_N" 标记，
    返回占位文本（演练不产生真实需求文件）。
    """
    try:
        from core.utils import load_config
        cfg = load_config()
        state = load_state()
        name = state.get("current_prompt_file")
        if not name:
            return {"ok": True, "name": None, "content": None, "message": "等待首轮需求"}
        if isinstance(name, str) and name.startswith(judge.MOCK_PROMPT_PREFIX):
            return {"ok": True, "name": name,
                    "content": "（MOCK 演练）这是演练占位需求——真实模式下，此处显示当前选手"
                               "要实现的 next_prompt.md 全文。",
                    "message": ""}
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


@app.post("/api/inject-rules")
async def api_inject_rules():
    """手动把测试规范写入 arena_repo/TESTING_GUIDELINES.md（幂等）。

    平时无需调用：arena 就绪时 /api/state 的刷新钩子已自动注入并自愈
    （文件被删会在下次刷新补回）。此接口供人类立即重注入时使用。
    """
    try:
        if not repo_ops.is_arena_ready():
            return {"ok": False, "injected": False, "path": None,
                    "message": "arena_repo 未就绪，无法注入规范"}
        from core import rules
        result = rules.inject_rules()  # 先注入（真实报告写入/已最新），再刷新 state
        repo_ops.refresh_arena_ready()
        result["ok"] = bool(result.get("ok"))
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


# ---------------------------------------------------------------------------
# 运维接口（选手元数据维护；与 arena_repo 无关，arena 未就绪时同样可用）
# ---------------------------------------------------------------------------

@app.post("/api/set-model")
async def api_set_model(payload: dict):
    """批量更新选手"实际模型"（模型迭代用；三字码/总称/历史记录不变）。

    请求体：{"updates": {"FBL": "Claude Fable 5.5", ...}}，键必须是已存在的三字码。
    校验失败整体拒绝（不做半截更新）；成功返回最新 state。
    """
    try:
        updates = (payload or {}).get("updates")
        if not isinstance(updates, dict) or not updates:
            return {"ok": False, "error": "请求体需为 {\"updates\": {\"三字码\": \"新模型名\"}}"}
        state = set_player_models(updates)
        return {"ok": True, "state": state, "count": len(updates)}
    except ValueError as e:  # 校验失败（未知三字码/空模型名）
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/draw")
async def api_draw():
    """顺序抽签：随机重排全部选手接力顺序并重置比赛进度（每场开始时点）。

    接力沿新顺序从 1 号位循环到最后再回 1。只重排顺序/重置进度；
    players 选手表与 current_prompt_file 首轮需求保留，arena 未就绪时同样可用。
    """
    try:
        state = draw_order()
        return {"ok": True, "state": state, "order": state.get("order"),
                "message": state.get("last_action_msg")}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/mock")
async def api_mock(payload: dict):
    """开关 MOCK 演练模式（写回 config.json，Web / CLI 同时生效）。

    开启后：验收答题 / 校验出题的判定结果改为随机模拟，状态推进与真实
    流程一致，但不跑 pytest、不调验题模型、不读写 arena_repo、不需要材料。
    用于人类上真实流程前预演整场操作。请求体：{"enabled": true/false}。
    """
    try:
        enabled = (payload or {}).get("enabled")
        if not isinstance(enabled, bool):
            return {"ok": False, "error": "请求体需为 {\"enabled\": true/false}"}
        set_config_flag("mock", enabled)
        log_event("mock", "MOCK 演练模式已%s（%s）" % (
            "开启" if enabled else "关闭",
            "随机模拟判定：不跑 pytest、不调验题模型、不碰 arena_repo" if enabled
            else "恢复真实评测"))
        return {"ok": True, "mock": enabled}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
