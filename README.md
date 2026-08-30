# Bug Relay（Bug 接力）评测框架

支撑 AI 编程接力竞技节目的 **评测框架 + Web 控制台 + CLI**。

> 本框架由 [GLM-5.3](https://z.ai) 创建。

> 本仓库只是 harness（评测框架）。**框架不碰 arena_repo 的业务逻辑，也不自动生成业务代码**；
> 框架永远不主动启动、追踪、观测任何选手 Agent，也不与选手对话——它只在人类执行 CLI
> 命令或点击 Web 按钮的瞬间工作：导入文件 → 跑 pytest → 记录结果 → 备份/回滚。

## 1. 仓库定位

| 仓库 | 说明 |
| ---- | ---- |
| `harness_repo`（本仓库） | 评测框架：FastAPI Web 控制台 + CLI + 裁判核心 |
| `arena_repo`（人类另建） | 真正被选手 AI 修改的业务代码仓库，**独立 git 仓库**，结构为 `src/`（业务代码，可由 `business_dir` 配置）+ `tests/`（历史 pytest 测试，一旦存在即锁定，禁止修改/删除） |

框架通过 `config.json` 的 `arena_repo_path`（或环境变量 `BUGRELAY_ARENA_REPO`，见 §2）指向 arena_repo（默认 `../arena_repo`，即两个仓库并排放）。
**arena_repo 不存在时，框架/CLI/Web 仍能正常启动**，只是相关操作显示"arena_repo 未就绪"，不会抛异常崩溃。

## 2. 安装与部署

依赖：fastapi、uvicorn、httpx、pytest、python-multipart（FastAPI 文件上传必需），可选 python-dotenv、rich（CLI 美化）。需要 **Python ≥ 3.9**。保持轻量。

```bash
python3 -m venv .venv && source .venv/bin/activate   # Ubuntu 系统 pip 受 PEP 668 限制，需用 venv
pip install -r requirements.txt
```

### Ubuntu 快速部署（开箱即用）

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/Vita0818/BugRelay-Harness.git
cd BugRelay-Harness
./run.sh                          # 自动建 venv + 装依赖 + 启动 http://127.0.0.1:8080
./run.sh 0.0.0.0 9000             # 自定义监听地址/端口
```

验题模型（出题校验时才需要，可选）：Ubuntu 上安装 Ollama 后执行 `ollama pull llama-3.1:8b`，`config.json` 的 `base_url` 保持 `http://localhost:11434/v1` 即可。

### 指定 arena_repo 路径（三种方式，优先级从高到低）

1. **环境变量**（换机器不改任何文件，适合脚本/systemd）：`export BUGRELAY_ARENA_REPO=/home/me/repos/arena_repo`
2. **config.json 的 `arena_repo_path`**：支持绝对路径、`~` 开头、或相对路径（相对本仓库根解析）
3. **默认 `../arena_repo`**：把 harness 与 arena_repo 两个仓库**并排克隆**即可直接用：

   ```
   repos/ $ git clone https://github.com/Vita0818/BugRelay-Harness.git
   repos/ $ git clone <你的 arena_repo.git>     # 与 harness 平级
   ```

路径无效或未就绪时页面只提示不崩溃；指向 harness 自身或其祖先/子目录会被安全策略拒绝。

## 3. 启动

```bash
# 方式一：CLI（推荐）
python -m cli.bugrelay web                 # 默认 127.0.0.1:8080
python -m cli.bugrelay web --host 0.0.0.0 --port 9000

# 方式二：直接 uvicorn
uvicorn app:app --host 127.0.0.1 --port 8080

# 方式三：一键脚本（自动 venv + 依赖，适合 Ubuntu）
./run.sh
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

### 5.2 每轮循环（每位选手固定四步：作答 → 判定 → 出题 → 自证）

控制台顶部有四步流程条，实时显示当前选手所处步骤（蓝=等待材料、绿=已完成、黄闪=评测运行中）：

| 步骤 | 谁在做 | 发生什么 |
| ---- | ---- | ---- |
| ① 作答 | 当前选手 | 按中栏需求改 arena_repo 业务代码，交付物落 `inbox/`（或人工上传） |
| ② 判定 | 框架（人类点「验收答题」） | 应用改动到 `src/`，跑历史+隐藏 pytest：全绿过；否则淘汰回滚 |
| ③ 出题 | 当前选手 | 交下一棒需求 `next_prompt.md` + 本轮隐藏测试 `hidden_tests.py` |
| ④ 自证 | 框架（人类点「校验出题并交棒」） | 复制仓库 → 验题模型仅凭需求单次重实现 → 隐藏测试考它：全绿才交棒，否则出题者淘汰 |

```
① 作答 → ② 判定 → ③ 出题 → ④ 自证
              │FAIL: 淘汰+回滚，换下一位          │FAIL: 出题者淘汰，换下一位
              ↓                                  ↓
        （下一位回到 ①）                   （下一位回到 ①，轮次+1 交棒）
```

Web 操作区对应：① 答题区（上传 `.zip`/多文件 → 「验收答题」）；② 出题区（上传需求 + 隐藏测试 → 「校验出题并交棒」）；③ 通用（刷新、「还原到最近备份」）。CLI `status` 同样显示当前步骤。

### 5.3 材料投递目录 inbox/（免手动上传，推荐）

**框架收材料本来就是文件级的**（Web 上传文件 / CLI 指路径），无需复制粘贴内容。inbox/
更进一步：把"挑文件"也省掉——Agent 按约定文件名交付，你只按一个按钮：

| 材料 | inbox/ 约定 | 触发方式 |
| ---- | ---- | ---- |
| 答题（业务代码改动） | `inbox/answer.zip` 或 `inbox/answer/` 目录 | 点「验收答题」（或 `judge-answer`），尚无已导入材料时自动拾取 |
| 出题（下一棒需求 + 隐藏测试） | `inbox/next_prompt.md` + `inbox/hidden_tests.py` | 点「校验出题并交棒」（或 `judge-proposal`），同上 |

- 已消费的材料自动移入 `inbox/_consumed/<时间戳>/` 留档，不会被下一轮误拾取；
- `config.json` 的 `inbox_dir` 可指向任意目录——**可直接配成你 Agent 的产物输出目录**，
  实现"Agent 写完 → 你点按钮"零手工；
- 建议在给 Agent 的提示词里明确约定交付物文件名（answer/、next_prompt.md、
  hidden_tests.py），Agent 是按提示词办事的，这一步由你控制；
- Web 顶栏与 `bugrelay status` 会显示 inbox 中检测到的材料；页面按钮在检测到材料时自动亮起。
- 首轮准备（prompts/ + hidden_tests/ + state）仍属人类赛前环节，不走 inbox。

### 5.4 CLI（与 Web 共用 core 裁判逻辑，结果一致）

```bash
python -m cli.bugrelay status                          # 查看轮次/存活/淘汰/arena_ready/inbox 材料
python -m cli.bugrelay load-answer <path>              # 导入答题文件（.zip / 目录 / 单文件）
python -m cli.bugrelay judge-answer                    # 验收答题（inbox/ 有材料时自动拾取）
python -m cli.bugrelay load-proposal <md> <py>         # 导入出题材料
python -m cli.bugrelay judge-proposal                  # 校验出题并交棒（inbox/ 有材料时自动拾取）
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
| `arena_repo_path` | arena_repo 位置（默认 `../arena_repo`；支持绝对路径、`~`、相对本仓库的路径）。可被环境变量 `BUGRELAY_ARENA_REPO` 覆盖（优先级更高） |
| `business_dir` / `history_tests_dir` | 业务目录（默认 `src`）与历史测试目录（默认 `tests`） |
| `verifier_model` | OpenAI 兼容 `/chat/completions`：`base_url`/`model`/`api_key`/`timeout_seconds`。**单次调用、不重试、超时直接判 FAIL** |
| `pytest_args` | 传给 pytest 的参数（默认 `["-q"]`，框架会追加 junitxml/legacy 等解析用参数） |
| `state_file` / `hidden_tests_dir` / `prompts_dir` / `backups_dir` | 框架自身目录布局 |
| `inbox_dir` | 材料投递目录（默认 `inbox`，可指向 Agent 的产物输出目录，见 §5.3） |

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
├── inbox/                # 材料投递目录（Agent 交付物落点，自动拾取，见 §5.3）
└── state/                # match.json + log.jsonl（最新在上）
```

`tmp/`（验题临时目录、上传暂存）为运行时按需创建，不在仓库中预置。

## 9. 故障排查

- **arena_repo 不存在**：页面顶部与文件树区会提示"arena_repo 未就绪"；确认 `config.json`
  的 `arena_repo_path`（或环境变量 `BUGRELAY_ARENA_REPO`）指向已存在的独立 git 仓库后点「刷新」。路径非法（指向 harness 自身或其
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
