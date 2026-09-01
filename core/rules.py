"""测试规范注入（把「测试规范」显式写进 arena_repo，让选手 Agent 看得见）。

设计：
- 规范文本 RULES_MARKDOWN 是唯一权威版本（与 README 第 9 节同源同义，面向选手改写）；
- arena_repo 接入框架（arena_ready 刷新为 True）时自动注入为
  arena_repo/TESTING_GUIDELINES.md；文件被删后下次刷新自动补回（自愈）；
- 内容一致则不重复写盘（幂等，GET /api/state 高频调用也不产生写放大）；
- 选手 Agent 打开仓库即可看到规范；人类把需求发给 Agent 时也可引用此文件。

边界：写入的是纯文档，不触碰 arena_repo 的业务代码与 tests/ 历史测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .utils import log_event
from .repo_ops import _safe_arena

# 注入到 arena_repo 根目录的文件名（大写醒目，选手 ls 即见）
RULES_FILENAME = "TESTING_GUIDELINES.md"

RULES_MARKDOWN = """# Bug Relay 测试规范（选手必读）

> 本文件由评测框架自动写入。你（选手 Agent）在本仓库的一切交付物都按本规范验收，
> 不合规的出题材料会在导入时被直接拒绝（不会进入验题）。

## 一、你在接力赛中要做的两件事

1. **答题**：根据上一棒留下的需求文档（next_prompt.md，由人类转交给你）修改本仓库
   的业务代码，让「全部历史测试 + 本轮隐藏测试」全绿。
2. **出题**（答题通过后）：给下一棒写新的需求文档 next_prompt.md，并附上对应的
   隐藏测试 hidden_tests.py。

## 二、答题规则

- **只改业务目录（`src/`）**。`tests/` 是历史测试，**锁定禁改禁删**——框架会在
  应用前后及 pytest 后比较内容哈希，动了即判「篡改历史测试」，直接淘汰并回滚。
- 不要动 `conftest.py`、`pytest.ini`、`pyproject.toml` 里的测试配置。
- 你的改动交付形式（由人类导入框架）：`answer.zip`（含改动的业务文件）或业务文件
  目录/单文件。包内路径即 `src/` 下的相对路径。
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
   缺陷，从不同角度夹住它。禁止在一个文件里测多个互不相关的问题——
   数量不对直接拒绝导入；引用多个业务模块会被警告。

   三个用例的推荐分工（同一缺陷的三个侧面）：

   | 用例 | 角度 |
   | --- | --- |
   | test_1 | 主路径：直接复现该缺陷会暴露的行为 |
   | test_2 | 边界/邻近值：缺陷两侧的正确行为仍须保持 |
   | test_3 | 回归防护：防止用特判糊弄过 test_1 的取巧修法 |

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
另一位同模型 Agent（或真人）按需求独立实现后也能通过，你的题才算可解。

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


def rules_injected() -> bool:
    """规范文件是否已在 arena_repo 中且内容为最新版。"""
    fp = rules_file_path()
    if fp is None or not fp.is_file():
        return False
    try:
        return fp.read_text(encoding="utf-8") == RULES_MARKDOWN
    except Exception:
        return False


def inject_rules() -> Dict:
    """把规范写入 arena_repo/TESTING_GUIDELINES.md（幂等：内容一致不写盘）。

    返回 {"ok", "injected", "path", "message"}；arena 未就绪时 ok=False。
    """
    fp = rules_file_path()
    if fp is None:
        return {"ok": False, "injected": False, "path": None,
                "message": "arena_repo 未就绪，无法注入规范"}
    try:
        if fp.is_file() and fp.read_text(encoding="utf-8") == RULES_MARKDOWN:
            return {"ok": True, "injected": False, "path": str(fp),
                    "message": "规范已是最新（%s）" % RULES_FILENAME}
        fp.write_text(RULES_MARKDOWN, encoding="utf-8")
        log_event("inject-rules", "测试规范已注入 arena_repo：%s" % RULES_FILENAME)
        return {"ok": True, "injected": True, "path": str(fp),
                "message": "已写入 %s（选手打开仓库即可见）" % RULES_FILENAME}
    except Exception as e:
        return {"ok": False, "injected": False, "path": str(fp),
                "message": "规范写入失败: %s" % e}
