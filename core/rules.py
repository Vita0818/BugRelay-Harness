"""项目指令注入（让 OpenCode 自动获得核心规则，并保留详细测试规范）。

设计：
- AGENTS_MANAGED_MARKDOWN 是 OpenCode 自动加载的项目级特权指令；
- RULES_MARKDOWN 是详细测试契约（与 README 第 9 节同源同义）；
- arena_repo 接入框架（arena_ready 刷新为 True）时自动注入为
  arena_repo/AGENTS.md + TESTING_GUIDELINES.md；文件被删后下次刷新自动补回；
- AGENTS.md 使用带标记的托管区块，保留 arena 自己已有的其它项目指令；
- 内容一致则不重复写盘（幂等，GET /api/state 高频调用也不产生写放大）；
- 每个 OpenCode 会话一开始就收到“一次只改/出一个问题”和“必须同模型自证”的核心约束。

边界：写入的是纯文档，不触碰 arena_repo 的业务代码与 tests/ 历史测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .utils import log_event
from .repo_ops import _safe_arena

# 注入到 arena_repo 根目录的文件名（大写醒目，选手 ls 即见）
RULES_FILENAME = "TESTING_GUIDELINES.md"
AGENTS_FILENAME = "AGENTS.md"
AGENTS_BLOCK_START = "<!-- BUG RELAY MANAGED INSTRUCTIONS START -->"
AGENTS_BLOCK_END = "<!-- BUG RELAY MANAGED INSTRUCTIONS END -->"

AGENTS_MANAGED_MARKDOWN = f"""{AGENTS_BLOCK_START}
# Bug Relay 选手强制规则

你正在参加 Bug Relay 编程接力赛。这些是项目级强制指令；无论使用哪一种模型，均须遵守。

## 开始前

- 立即完整阅读根目录 `TESTING_GUIDELINES.md`，其中的测试格式和交付契约同样强制。
- 人类粘贴给你的当前提示词决定本次任务；不要自行扩大任务范围。

## 一次只处理一个问题

- **答题时只修当前提示词描述的一个问题。** 不顺手修其它缺陷，不增加无关功能。一个问题
  可以很复杂、横跨多个模块，也可以需要必要的架构调整；允许这些改动，但每一处改动都必须
  服务于同一个目标，不能借机混入第二个无关问题。所有历史行为必须继续通过。
- **出题时只提出一个清晰、独立、可观察的问题。** `next_prompt.md` 只能描述这一项需求；
  `hidden_tests.py` 的三条测试必须全部针对同一个问题。题目可以涉及多个文件或模块，判断
  标准是“是否只有一个验收目标”，不是“改了几个模块”；禁止捆绑第二个无关缺陷。
- 不得为了出下一题而修改正式业务代码；下一题由提示词和隐藏测试定义。

## 自证限制题目难度

- 新题必须先满足：当前正式代码的历史测试全绿，而三条新测试全部 RED。
- 随后人类会在一个不含新隐藏测试的独立副本中，启动**全新的同模型 Agent**，只把
  `next_prompt.md` 粘贴给它。该 Agent 必须能读取当前环境并在一次正常会话内完成任务。
- 完成后，历史测试和三条新测试必须全部 GREEN，这道题才有效；否则出题者失败。
- 因此题目必须范围集中、说明完整、边界明确且现实可解。禁止依赖隐藏知识、外部服务、
  随机/时间状态、未提供的数据或只有出题者自己知道的实现技巧；不要故意出过难或含糊的题。

## 文件边界

- 只修改人类当前阶段允许的业务或交付目录；绝不修改 `tests/`、`conftest.py`、
  `pytest.ini`、`pyproject.toml`、`AGENTS.md` 或 `TESTING_GUIDELINES.md`。
- 必须把产物实际保存到人类指定的目录和文件名，不能只在聊天中贴出代码。
{AGENTS_BLOCK_END}
"""

RULES_MARKDOWN = """# Bug Relay 测试规范（选手必读）

> 本文件由评测框架自动写入。你（选手 Agent）在本仓库的一切交付物都按本规范验收，
> 不合规的出题材料会在导入时被直接拒绝（不会进入验题）。

## 一、你在接力赛中要做的两件事

1. **答题**：根据上一棒留下的需求文档（next_prompt.md，由人类转交给你）修改本仓库
   的业务代码，让「全部历史测试 + 本轮隐藏测试」全绿。只修提示词指定的一个问题，
   不顺手修其它缺陷、不添加无关功能。允许为这个问题修改多个模块或进行必要重构，
   但所有改动必须服务于同一个验收目标。
2. **出题**（答题通过后）：给下一棒写新的需求文档 next_prompt.md，并附上对应的
   隐藏测试 hidden_tests.py。

## 二、答题规则

- **只改业务目录（`src/`）**。`tests/` 是历史测试，**锁定禁改禁删**——框架会在
  应用前后及 pytest 后比较内容哈希，动了即判「篡改历史测试」，直接淘汰并回滚。
- 不要动 `conftest.py`、`pytest.ini`、`pyproject.toml` 里的测试配置。
- 把改动实际保存到人类指定的交付目录：`answer.zip`（含改动的业务文件）或 `answer/`
  目录。Harness 会自动读取；不要只在聊天回复里展示代码。
- 想本地自测：在仓库根目录跑 `python -m pytest`（仓库根已在 PYTHONPATH 中）。

## 三、出题规则（hidden_tests.py 硬性规范）

违反任一条会被静态闸门**拒绝导入**：

1. **单文件自包含**：一个 `hidden_tests.py` 就是全部。
   - 禁止 `import tests...` / `from tests... import ...`（历史测试与你无关）；
   - 禁止相对导入 `from . import ...` / `from .. import ...`
     （验题时你的文件会被单独拷进一个全新临时仓库，任何外部依赖都会挂）；
   - 禁止附带数据文件——**测试数据全部内联写在测试函数里**。
2. **三个模块顶层普通函数**：不要嵌套、不要放进测试类、不要使用 async、参数化、
   fixture 参数、`@pytest.fixture`、skip 或 xfail。每个 `test_*` 都必须无参数，数据
   在函数体内自行构造；三个函数必须对应 pytest 实际收集到的三条测试。
3. **必须有真测试**：每个 `test_*` 函数都包含 `assert` 或 `pytest.raises`
   断言；缺少真断言会被静态闸门直接拒绝。
4. **语法合法**，编码 UTF-8。
5. **一题一缺陷、三测同源**：每次出题只引入 **一个** 缺陷（需求变更）；
   `hidden_tests.py` 必须恰好包含 **3 个** `test_*` 函数，全部针对这同一个
   缺陷，从不同角度夹住它。一个问题可以复杂并横跨多个模块；模块数量不是判定标准。
   禁止把多个互不相关的验收目标塞进同一道题，测试数量不对直接拒绝导入。

   三个用例的推荐分工（同一缺陷的三个侧面）：

   | 用例 | 角度 |
   | --- | --- |
   | test_1 | 主路径：直接复现该缺陷会暴露的行为 |
   | test_2 | 边界/邻近值：缺陷两侧的正确行为仍须保持 |
   | test_3 | 回归防护：防止用特判糊弄过 test_1 的取巧修法 |

6. **难度必须通过同模型自证**：新测试在当前正式代码上必须全部 RED；随后人类会在
   不含新隐藏测试的独立副本中启动一位全新的同模型 Agent，只把 `next_prompt.md`
   交给它。该 Agent 必须能在一次正常会话内完成，且历史 + 新测试全部 GREEN，题目才有效。
   所以需求必须自包含、范围集中、边界明确、现实可解；过难、含糊、依赖隐藏知识或要求
   多项无关改动的题都会使出题者失败。

强烈建议（静态查不出，但验题/复跑会翻车）：

- **确定性**：同一份代码跑两遍结果一致。不用随机（必须用就固定 seed）、不依赖
  当前时间/时区、不依赖字典遍历顺序；
- **无外部依赖**：不访问网络、不读写仓库外文件、不启动进程/端口；
- **快**：单个用例 < 1 秒（框架有全局 pytest 超时，超时直接判 FAIL）；
- **独立**：用例彼此不依赖执行顺序、不共享可变全局状态。

## 四、测试数据从哪来

**全部内联构造**。示例：

```python
from src.kv import KVStore  # 唯一正确的导入姿势：包路径前缀


def test_get_after_put():          # 主路径：缺陷直接暴露处
    kv = KVStore()
    kv.put("用户A", {"score": 100})
    assert kv.get("用户A") == {"score": 100}


def test_put_overwrite():          # 边界：邻近行为必须仍然正确
    kv = KVStore()
    kv.put("k", 1)
    kv.put("k", 2)
    assert kv.get("k") == 2


def test_other_keys_unaffected():  # 回归防护：防特判糊弄
    kv = KVStore()
    kv.put("x", 1)
    kv.put("y", 2)
    assert kv.get("x") == 1
    assert kv.get("y") == 2
    assert kv.get("不存在") is None
```

出题思路：用「构造数据 + 断言行为」代替「加载现成数据」；三个用例从三个角度
夹住**同一个**缺陷。边界值、空态、异常路径都是好题；纯正常路径的题太弱，
另一位全新的同模型 Agent（或真人）只看当前环境和需求、看不到新隐藏测试，也能独立
实现并让全部测试通过，你的题才算可解。这一环节专门防止题目过难、含糊或不可复现。

## 五、框架如何跑你的测试（执行规范）

| 项 | 规则 |
| --- | --- |
| 命令 | `python -m pytest -q --tb=short`（在仓库根目录） |
| 发现 | pytest 默认规则：`test_*.py` / `*_test.py` |
| PYTHONPATH | 仓库根（所以 `from src...` 直接可用，别再 sys.path.insert） |
| 隐藏测试注入 | 验收时拷入 `tests/hidden_tests.py`，与历史测试同场运行 |
| 答题全绿 | 历史测试全部通过；本轮隐藏测试实际收集 3 条、全部通过、无 skip/error |
| 超时 | 全局超时，超时判 FAIL |
| 出题全红 | 当前正式代码的历史测试仍全绿；你新出的 3 条测试必须全部断言失败 |
| 手动自证 | 全红后框架创建不含新测试的独立目录；人类手动启动全新的同模型 Agent
  （或真人）并粘贴 next_prompt.md；完成后框架注入同一份新测试，历史+新测试全绿才有效 |

## 六、交付物文件名（写给人类/Agent 的交付约定）

| 交付物 | 文件名 |
| --- | --- |
| 答题改动 | `answer.zip` 或 `answer/` 目录 |
| 下棒需求 | `next_prompt.md` |
| 隐藏测试 | `hidden_tests.py` |
"""


def rules_file_path() -> Optional[Path]:
    """arena 仓库内的规范文件绝对路径；arena 不可用时返回 None。"""
    p, err = _safe_arena()
    if p is None:
        return None
    return p / RULES_FILENAME


def agents_file_path() -> Optional[Path]:
    """arena 仓库内 OpenCode 自动加载的项目指令文件。"""
    p, err = _safe_arena()
    if p is None:
        return None
    return p / AGENTS_FILENAME


def _merge_agents_markdown(existing: str) -> str:
    """插入或更新 Bug Relay 托管区块，同时保留 arena 自己的其它 AGENTS.md 内容。"""
    has_start = AGENTS_BLOCK_START in existing
    has_end = AGENTS_BLOCK_END in existing
    if has_start != has_end:
        raise ValueError("AGENTS.md 的 Bug Relay 托管区块标记不完整，请人工修复后重试")
    if has_start:
        start = existing.index(AGENTS_BLOCK_START)
        end = existing.index(AGENTS_BLOCK_END, start) + len(AGENTS_BLOCK_END)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].lstrip()
        pieces = [part for part in (prefix, AGENTS_MANAGED_MARKDOWN.strip(), suffix) if part]
        return "\n\n".join(pieces) + "\n"
    prefix = existing.rstrip()
    return ((prefix + "\n\n") if prefix else "") + AGENTS_MANAGED_MARKDOWN.strip() + "\n"


def _agents_rules_current(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        current = path.read_text(encoding="utf-8")
        return _merge_agents_markdown(current) == current
    except Exception:
        return False


def rules_injected() -> bool:
    """详细规范和 OpenCode 项目指令是否都已注入且为最新版。"""
    fp = rules_file_path()
    ap = agents_file_path()
    if fp is None or ap is None or not fp.is_file():
        return False
    try:
        return fp.read_text(encoding="utf-8") == RULES_MARKDOWN and _agents_rules_current(ap)
    except Exception:
        return False


def inject_rules() -> Dict:
    """注入 TESTING_GUIDELINES.md 和 AGENTS.md 托管区块（幂等）。

    返回 {"ok", "injected", "path", "agents_path", "message"}；arena 未就绪时 ok=False。
    """
    fp = rules_file_path()
    ap = agents_file_path()
    if fp is None or ap is None:
        return {"ok": False, "injected": False, "path": None, "agents_path": None,
                "message": "arena_repo 未就绪，无法注入规范"}
    try:
        changed: list[str] = []
        if not fp.is_file() or fp.read_text(encoding="utf-8") != RULES_MARKDOWN:
            fp.write_text(RULES_MARKDOWN, encoding="utf-8")
            changed.append(RULES_FILENAME)

        existing_agents = ap.read_text(encoding="utf-8") if ap.is_file() else ""
        merged_agents = _merge_agents_markdown(existing_agents)
        if merged_agents != existing_agents:
            ap.write_text(merged_agents, encoding="utf-8")
            changed.append(AGENTS_FILENAME)

        if not changed:
            return {"ok": True, "injected": False, "path": str(fp),
                    "agents_path": str(ap),
                    "message": "OpenCode 项目指令与测试规范均已是最新"}
        log_event("inject-rules", "已注入 arena_repo：%s" % "、".join(changed))
        return {"ok": True, "injected": True, "path": str(fp),
                "agents_path": str(ap),
                "message": "已更新 %s；OpenCode 新会话会自动加载 AGENTS.md" % "、".join(changed)}
    except Exception as e:
        return {"ok": False, "injected": False, "path": str(fp), "agents_path": str(ap),
                "message": "规范写入失败: %s" % e}
