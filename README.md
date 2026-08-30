# Bug Relay（Bug 接力）评测框架

支撑 AI 编程接力竞技节目的 **评测框架 + Web 控制台 + CLI**。

> 本仓库只是 harness（评测框架）。**框架不碰 arena_repo 的业务逻辑，也不自动生成业务代码**；
> 框架永远不主动启动、追踪、观测任何选手 Agent，也不与选手对话——它只在人类执行 CLI
> 命令或点击 Web 按钮的瞬间工作：导入文件 → 跑 pytest → 记录结果 → 备份/回滚。

## 1. 仓库定位

| 仓库 | 说明 |
| ---- | ---- |
| `harness_repo`（本仓库） | 评测框架：FastAPI Web 控制台 + CLI + 裁判核心 |
| `arena_repo`（人类另建） | 真正被选手 AI 修改的业务代码仓库，**独立 git 仓库**，结构为 `src/`（业务代码，可由 `business_dir` 配置）+ `tests/`（历史 pytest 测试，一旦存在即锁定，禁止修改/删除） |

框架通过 `config.json` 的 `arena_repo_path` 指向 arena_repo（默认 `../arena_repo`）。
**arena_repo 不存在时，框架/CLI/Web 仍能正常启动**，只是相关操作显示"arena_repo 未就绪"，不会抛异常崩溃。

## 2. 安装

```bash
pip install -r requirements.txt
```

依赖：fastapi、uvicorn、httpx、pytest、python-multipart（FastAPI 文件上传必需），可选 python-dotenv、rich（CLI 美化）。保持轻量。

## 3. 启动

```bash
# 方式一：CLI（推荐）
python -m cli.bugrelay web                 # 默认 127.0.0.1:8080
python -m cli.bugrelay web --host 0.0.0.0 --port 9000

# 方式二：直接 uvicorn
uvicorn app:app --host 127.0.0.1 --port 8080
```

浏览器打开 http://127.0.0.1:8080 即为单页控制台。

## 4. 赛制与裁判逻辑（实现说明）

选手默认 `["A","B","C","D"]` 轮流接力，每轮三步：

1. **答题**：当前选手按上一棒留下的 `next_prompt.md`（仅需求，不含测试）修改 arena_repo 业务代码。
   人类导入其改动后点「验收答题」→ 框架备份 → 只把上传内容应用到 `src/`（严禁触碰 `tests/`，
   应用前后用 git 快照对比，检测到篡改立即回滚并判 FAIL）→ 跑「历史 + 本轮隐藏」pytest →
   全绿：隐藏测试永久移入 `tests/` 成为历史测试、代码保留、再备份；否则：回滚、选手淘汰、切换下一位。
   **答题环节不调用任何模型。**
2. **出题**：答题通过后，选手提交给下一棒的需求文档 `next_prompt.md` + 隐藏测试 `hidden_tests.py`，
   人类导入后点「校验出题并交棒」。
3. **验题（自证）**：框架复制 arena_repo 到全新临时目录 `tmp/relay_<uuid>`，把需求**单次**喂给
   验题模型（OpenAI 兼容 `/chat/completions`，不重试，超时即 FAIL），要求它仅凭需求重新实现一遍；
   模型输出按 `=== FILE: 相对路径 ===` 文件块解析写入临时目录（无任何文件块直接判 FAIL）；
   再把选手的 hidden_tests.py 拷入临时目录 `tests/` 跑 pytest（历史+隐藏）→ 全绿：需求存入
   `prompts/`、新隐藏测试落入 `hidden_tests/`、轮次 +1、交棒；否则：回滚（恢复最近成功备份，
   即出题者答题通过时的状态——**其已通过验收的代码依赛制保留**）、出题者淘汰、切换下一位。

规则：历史测试永增不减；淘汰只取消比赛资格，此前成功提交的代码留在 arena_repo 中成为后人的环境。

## 5. 操作流程

### 5.1 首轮（人类准备）

1. 人类另建独立 git 仓库 `arena_repo`（与 harness_repo 平级），含 `src/`（业务代码）与 `tests/`（初始历史测试，可为空目录）。**历史测试一旦存在即锁定。**
2. 把首轮需求 `next_prompt.md` 放入 `prompts/`（任意文件名，如 `round_1.md`），首轮隐藏测试 `hidden_tests.py` 放入 `hidden_tests/`。
3. 在 `state/match.json` 中把 `current_prompt_file` 设为该需求文件名（如 `"round_1"` 或 `"round_1.md"`，框架按文件名在 `prompts/` 下查找）。
   > 选手只能看到 `next_prompt.md`（Web 中栏完整展示），**永远看不到 hidden_tests/ 的任何内容**。

### 5.2 每轮循环

```
选手改码 → 人类导入答题文件（上传/CLI load-answer）→ 点「验收答题」
    ├─ PASS → 该选手出题 → 导入 next_prompt.md + hidden_tests.py（/api/proposal）→ 点「校验出题并交棒」
    │           ├─ PASS（模型自证全绿）→ 轮次 +1，交棒给下一位，回到"选手改码"
    │           └─ FAIL → 出题者淘汰，切换下一位（其已验收代码保留）
    └─ FAIL → 选手淘汰、代码回滚，切换下一位
```

Web 操作区对应：① 答题区（上传 `.zip`/多文件 → 「验收答题」）；② 出题区（上传需求 + 隐藏测试 → 「校验出题并交棒」）；③ 通用（刷新、「还原到最近备份」）。

### 5.3 CLI（与 Web 共用 core 裁判逻辑，结果一致）

```bash
python -m cli.bugrelay status                          # 查看轮次/存活/淘汰/arena_ready
python -m cli.bugrelay load-answer <path>              # 导入答题文件（.zip / 目录 / 单文件）
python -m cli.bugrelay judge-answer                    # 验收答题
python -m cli.bugrelay load-proposal <md> <py>         # 导入出题材料
python -m cli.bugrelay judge-proposal                  # 校验出题并交棒
python -m cli.bugrelay restore                         # 还原最近备份
python -m cli.bugrelay web                             # 启动 Web
```

> CLI 没有任何"自动跑选手"的命令；框架永远不会代替选手答题或出题。

## 6. Web API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/state` | 赛况（轮次/选手/存活/淘汰/arena_ready/最近结果） |
| GET | `/api/tree` | arena_repo 文件树（**已过滤 hidden_tests/**、.git、__pycache__ 等） |
| GET | `/api/file?path=…` | 查看文件内容（前端渲染行号；拒绝路径穿越与 hidden_tests） |
| GET | `/api/log` | 操作日志（最新在上；只有框架操作文字，不含测试内容） |
| GET | `/api/prompt` | 当前需求 next_prompt.md 内容 |
| POST | `/api/answer` | 上传答题文件（.zip 或多文件/文件夹） |
| POST | `/api/judge-answer` | 验收答题 |
| POST | `/api/proposal` | 上传 next_prompt.md + hidden_tests.py |
| POST | `/api/judge-proposal` | 校验出题并交棒 |
| POST | `/api/restore` | 还原最近备份 |

安全约定：**前端任何接口都不返回 hidden_tests/ 内容**；测试结果只显示总结果与
通过数/总数，测试函数名、断言内容、diff 一律不展示（pytest 原始输出只留在服务端终端供人类排障）。

## 7. 配置（config.json）

| 键 | 说明 |
| ---- | ---- |
| `arena_repo_path` | arena_repo 位置（相对本仓库，默认 `../arena_repo`） |
| `business_dir` / `history_tests_dir` | 业务目录（默认 `src`）与历史测试目录（默认 `tests`） |
| `verifier_model` | OpenAI 兼容 `/chat/completions`：`base_url`/`model`/`api_key`/`timeout_seconds`。**单次调用、不重试、超时直接判 FAIL** |
| `pytest_args` | 传给 pytest 的参数（默认 `["-q"]`，框架会追加 junitxml/legacy 等解析用参数） |
| `state_file` / `hidden_tests_dir` / `prompts_dir` / `backups_dir` | 框架自身目录布局 |

## 8. 目录结构

```
harness_repo/
├── app.py                # FastAPI 应用
├── requirements.txt
├── config.json
├── cli/bugrelay.py       # argparse CLI
├── core/
│   ├── judge.py          # 裁判核心：pytest、备份、回滚、答题验收、出题验题（唯一调用模型处）
│   ├── repo_ops.py       # arena_repo 复制、git 状态、还原、上传应用
│   └── utils.py          # 配置/状态/日志
├── templates/index.html  # 单页控制台
├── static/               # style.css + main.js
├── hidden_tests/         # 暂存本轮隐藏测试（评测后清理/归档进 arena tests/）
├── backups/              # arena_repo 备份（index.json 登记，会增长，可手动清理旧目录及登记项）
├── prompts/              # 每轮合法的 next_prompt.md
└── state/                # match.json + log.jsonl（最新在上）
```

`tmp/`（验题临时目录、上传暂存）为运行时按需创建，不在仓库中预置。

## 9. 故障排查

- **arena_repo 不存在**：页面顶部与文件树区会提示"arena_repo 未就绪"；确认 `config.json`
  的 `arena_repo_path` 指向已存在的独立 git 仓库后点「刷新」。路径非法（指向 harness 自身或其
  祖先/子目录）也会被判为未就绪，属安全防护。
- **验题模型连不上**：`judge-proposal` 会按 `timeout_seconds` 超时直接判 FAIL（不重试）；
  请确认 `base_url`（如 Ollama 需 `http://localhost:11434/v1`）与 `model` 名称。
- **pytest 路径问题**：框架在仓库根目录以 `python -m pytest` 运行并把仓库根加入
  `PYTHONPATH`；若历史测试仍找不到被测代码，检查 arena_repo 的测试导入方式与
  `pytest_args` 配置。
- **首轮验收报"本轮隐藏测试缺失"**：需人工把该轮 `hidden_tests.py` 放入 `hidden_tests/`。
  另注：若某选手出题失败被淘汰，本轮新需求被拒收，下一棒将面对旧需求继续（此时
  `hidden_tests/` 可能为空，同样需人工补充后再验收）。
- **backups/ 增长**：每次验收都会全量备份 arena_repo；可手动删除旧备份目录并同步
  从 `backups/index.json` 移除对应登记项。

## 10. 边界声明

- 本框架仓库**不包含任何被评测的业务代码**，也不生成 demo 业务数据；
- 框架**不创建** arena_repo（由人类另建），只在人类触发评测时对既有仓库做
  受控的备份/应用/回滚；
- 历史测试一经进入 arena_repo 的 `tests/` 即锁定：上传内容顶层 `tests/` 目录会被
  整体拦截，git 快照对比发现改动立即回滚并判 FAIL。
