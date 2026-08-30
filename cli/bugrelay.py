"""Bug Relay CLI（规范【7】）。

用法（在本仓库根目录）：
    python -m cli.bugrelay web                          # 启动 Web 控制台
    python -m cli.bugrelay status                       # 打印当前赛况
    python -m cli.bugrelay load-answer <path>           # 导入答题文件（zip/目录/单文件）
    python -m cli.bugrelay judge-answer                 # 验收答题
    python -m cli.bugrelay load-proposal <md> <py>      # 导入出题材料
    python -m cli.bugrelay judge-proposal              # 校验出题并交棒
    python -m cli.bugrelay restore                     # 还原最近备份
    python -m cli.bugrelay set-model FBL "Claude 5.5"  # 更新选手实际模型（模型迭代）
    python -m cli.bugrelay draw                        # 顺序抽签（每场开始时）
    python -m cli.bugrelay inject-rules                # 手动注入测试规范到 arena_repo

说明：
- 与 Web 共用 core/ 函数，操作结果一致；
- 不实现任何"自动跑选手/自动对话"的命令（框架只在人类执行命令的瞬间工作）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许 `python cli/bugrelay.py ...` 直接运行：把仓库根目录加进 sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import judge, repo_ops          # noqa: E402
from core.utils import draw_order, ensure_dirs, load_state, set_player_models  # noqa: E402

try:  # rich 可选，仅美化 CLI 输出
    from rich.console import Console
    from rich.table import Table
    _CONSOLE = Console()
except Exception:  # pragma: no cover
    _CONSOLE = None


def _print(msg: str = "") -> None:
    if _CONSOLE is not None:
        _CONSOLE.print(msg)
    else:
        print(msg)


def _bootstrap() -> None:
    """每个子命令执行前的准备：目录/初始 state + 刷新 arena_ready。"""
    ensure_dirs()
    repo_ops.refresh_arena_ready()


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _step_label(state) -> str:
    """四步流程（作答→判定→出题→自证）中当前所在步骤（等待材料的环节）。"""
    return "3/4 出题（等待需求+隐藏测试）" if state.get("phase") == "proposing" else "1/4 作答（等待业务代码改动）"


def _player_label(state, code) -> str:
    """选手显示：三字码 · 总称（实际模型）。"""
    info = (state.get("players") or {}).get(code)
    if not info:
        return str(code)
    label = "%s · %s" % (code, info.get("name", ""))
    if info.get("model"):
        label += "（%s）" % info["model"]
    return label


def cmd_status(_args) -> int:
    _bootstrap()
    state = load_state()
    ib = judge.inbox_status()
    inbox_parts = []
    if ib.get("answer"):
        inbox_parts.append("答题材料")
    if ib.get("proposal"):
        inbox_parts.append("出题材料")
    if _CONSOLE is not None:
        table = Table(title="Bug Relay 赛况", show_header=True, header_style="bold")
        table.add_column("项目", style="cyan")
        table.add_column("值")
        table.add_row("状态", str(state.get("status")))
        table.add_row("轮次", str(state.get("round")))
        table.add_row("当前选手", _player_label(state, state.get("current_player")))
        table.add_row("当前步骤", "作答→判定→出题→自证，当前：%s" % _step_label(state))
        table.add_row("阶段", "answering=待验收答题 / proposing=待校验出题 -> %s" % state.get("phase"))
        surv = state.get("survivors") or []
        table.add_row("存活", "%d 人：%s" % (len(surv), ", ".join(surv)))
        elim = state.get("eliminated") or []
        table.add_row("淘汰", ("%d 人：%s" % (len(elim), ", ".join(elim))) if elim else "（无）")
        table.add_row("arena_ready", "就绪" if state.get("arena_ready") else "未就绪（arena_repo 不存在或不是 git 仓库）")
        table.add_row("规范注入", ("已注入（TESTING_GUIDELINES.md 在仓库中）" if state.get("rules_injected")
                                   else ("未注入（arena 就绪后自动补）" if state.get("arena_ready") else "未注入（arena 未就绪）")))
        table.add_row("inbox 材料", "、".join(inbox_parts) or "（空）")
        table.add_row("当前需求", str(state.get("current_prompt_file")) or "（等待首轮需求）")
        table.add_row("最近结果", str(state.get("last_result")))
        scores = state.get("scores") or {}
        table.add_row("积分", ", ".join("%s:%s" % (k, v) for k, v in scores.items()) or "（无）")
        table.add_row("说明", str(state.get("last_action_msg") or ""))
        _CONSOLE.print(table)
    else:
        print("状态      : %s" % state.get("status"))
        print("轮次      : %s" % state.get("round"))
        print("当前选手  : %s" % _player_label(state, state.get("current_player")))
        print("当前步骤  : %s" % _step_label(state))
        print("阶段      : %s" % state.get("phase"))
        surv = state.get("survivors") or []
        print("存活      : %d 人：%s" % (len(surv), ", ".join(surv)))
        elim = state.get("eliminated") or []
        print("淘汰      : %s" % (("%d 人：%s" % (len(elim), ", ".join(elim))) if elim else "（无）"))
        print("arena     : %s" % ("就绪" if state.get("arena_ready") else "未就绪"))
        print("规范注入  : %s" % ("已注入" if state.get("rules_injected") else "未注入"))
        print("inbox     : %s" % ("、".join(inbox_parts) or "（空）"))
        print("当前需求  : %s" % (state.get("current_prompt_file") or "（等待首轮需求）"))
        print("最近结果  : %s" % state.get("last_result"))
        print("说明      : %s" % (state.get("last_action_msg") or ""))
    return 0


def cmd_set_model(args) -> int:
    """更新选手实际模型（等同 /api/set-model；三字码/总称/历史记录不变）。"""
    _bootstrap()
    try:
        st = set_player_models({args.code: args.model})
    except ValueError as e:
        _print("更新失败：%s" % e)
        return 1
    info = (st.get("players") or {}).get(args.code) or {}
    _print("已更新：%s · %s（%s）" % (args.code, info.get("name", ""), info.get("model", "")))
    return 0


def cmd_draw(_args) -> int:
    """顺序抽签（等同 /api/draw）：随机重排接力顺序并重置比赛进度（每场开始时用）。"""
    _bootstrap()
    try:
        st = draw_order()
    except ValueError as e:
        _print("抽签失败：%s" % e)
        return 1
    order = st.get("order") or []
    players = st.get("players") or {}
    if _CONSOLE is not None:
        table = Table(title="抽签结果（从 1 号接力到最后一位，再回到 1 号循环）")
        table.add_column("位次", justify="right", style="cyan")
        table.add_column("三字码", style="bold")
        table.add_column("总称")
        for i, code in enumerate(order):
            table.add_row(str(i + 1), code, (players.get(code) or {}).get("name", ""))
        _CONSOLE.print(table)
    else:
        print("抽签结果（从 1 号接力到最后一位，再回到 1 号循环）：")
        for i, code in enumerate(order):
            print("%2d. %s %s" % (i + 1, code, (players.get(code) or {}).get("name", "")))
    return 0


def cmd_inject_rules(_args) -> int:
    """手动注入测试规范到 arena_repo/TESTING_GUIDELINES.md（等同 /api/inject-rules）。

    平时无需执行：CLI 每次运行前的 _bootstrap 刷新钩子已自动注入并自愈。
    """
    _bootstrap()
    from core import rules
    r = rules.inject_rules()
    _print(r.get("message") or ("失败: %s" % r.get("error", "")) or "未知结果")
    return 0 if r.get("ok") else 1


def cmd_web(args) -> int:
    """启动 uvicorn（等价：uvicorn app:app --host ... --port ...）。"""
    import uvicorn
    import app as app_module
    _print("[bugrelay] 启动 Web 控制台: http://%s:%s" % (args.host, args.port))
    uvicorn.run(app_module.app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_load_answer(args) -> int:
    _bootstrap()
    result = judge.import_answer(args.path)
    _print("导入答题: %s | %s" % ("成功" if result.get("ok") else "失败",
                                 result.get("message") or result.get("error")))
    return 0 if result.get("ok") else 1


def cmd_judge_answer(_args) -> int:
    _bootstrap()
    result = judge.verify_answer()
    _render_judge_result(result, "验收答题")
    return 0 if result.get("ok") else 1


def cmd_load_proposal(args) -> int:
    _bootstrap()
    result = judge.import_proposal(args.prompt_file, args.test_file)
    _print("导入出题材料: %s | %s" % ("成功" if result.get("ok") else "失败",
                                    result.get("message") or result.get("error")))
    return 0 if result.get("ok") else 1


def cmd_judge_proposal(_args) -> int:
    _bootstrap()
    result = judge.verify_proposal()
    _render_judge_result(result, "校验出题")
    return 0 if result.get("ok") else 1


def cmd_restore(_args) -> int:
    _bootstrap()
    result = judge.restore_arena()
    _print("还原: %s | %s" % ("成功" if result.get("ok") else "失败",
                           result.get("message") or result.get("error")))
    return 0 if result.get("ok") else 1


def _render_judge_result(result: dict, title: str) -> None:
    """渲染判定结果（只含总结果与计数，与 Web 前端同一口径）。"""
    if not result.get("ok"):
        _print("%s 未执行: %s" % (title, result.get("reason") or result.get("error")))
        return
    verdict = result.get("result")
    icon = "PASS" if verdict == "PASS" else "FAIL"
    _print("[%s] %s -> %s" % (icon, title, result.get("message") or ""))
    if result.get("history") is not None:
        h, d = result.get("history"), result.get("hidden")
        _print("  历史测试: %s/%s   隐藏测试: %s/%s" % (h.get("passed"), h.get("total"),
                                                       d.get("passed"), d.get("total")))
    if result.get("reason"):
        _print("  原因: %s" % result["reason"])


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugrelay",
        description="Bug Relay 评测框架 CLI（与 Web 控制台共用 core 裁判逻辑）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_web = sub.add_parser("web", help="启动 Web 控制台（uvicorn）")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8080)
    p_web.set_defaults(func=cmd_web)

    p_status = sub.add_parser("status", help="打印当前 state（轮次/存活/淘汰/arena_ready）")
    p_status.set_defaults(func=cmd_status)

    p_load = sub.add_parser("load-answer", help="导入答题文件（等同 /api/answer）")
    p_load.add_argument("path", help="选手返回的 .zip / 目录 / 单个业务文件")
    p_load.set_defaults(func=cmd_load_answer)

    p_judge = sub.add_parser("judge-answer", help="验收答题（等同 /api/judge-answer；inbox/ 有材料时自动拾取）")
    p_judge.set_defaults(func=cmd_judge_answer)

    p_prop = sub.add_parser("load-proposal", help="导入出题材料（等同 /api/proposal）")
    p_prop.add_argument("prompt_file", help="next_prompt.md（.md/.txt）")
    p_prop.add_argument("test_file", help="hidden_tests.py")
    p_prop.set_defaults(func=cmd_load_proposal)

    p_jprop = sub.add_parser("judge-proposal", help="校验出题并交棒（等同 /api/judge-proposal；inbox/ 有材料时自动拾取）")
    p_jprop.set_defaults(func=cmd_judge_proposal)

    p_restore = sub.add_parser("restore", help="还原 arena_repo 到最近备份（等同 /api/restore）")
    p_restore.set_defaults(func=cmd_restore)

    p_model = sub.add_parser(
        "set-model", help="更新选手实际模型（等同 /api/set-model；模型迭代用，部署后无需改文件）")
    p_model.add_argument("code", help="选手三字码，如 FBL")
    p_model.add_argument("model", help="新的实际模型名，如 'Claude Fable 5.5'")
    p_model.set_defaults(func=cmd_set_model)

    p_draw = sub.add_parser(
        "draw", help="顺序抽签：随机重排接力顺序并重置比赛进度（等同 /api/draw；每场开始时用）")
    p_draw.set_defaults(func=cmd_draw)

    p_rules = sub.add_parser(
        "inject-rules", help="手动注入测试规范到 arena_repo/TESTING_GUIDELINES.md（幂等；平时自动注入）")
    p_rules.set_defaults(func=cmd_inject_rules)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
