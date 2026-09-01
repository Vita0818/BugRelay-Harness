# Bug Relay（Bug 接力）评测框架

支撑 AI 编程接力竞技视频录制的 **评测框架 + Web 流程控制台 + CLI**。

> 本框架由 [GLM-5.3](https://z.ai) 创建。

> 本仓库只是 harness（评测框架）。**框架不碰 arena_repo 的业务逻辑，也不自动生成业务代码**；
> 框架永远不主动启动、追踪、观测任何选手 Agent，也不与选手对话——它只在人类执行 CLI
> 命令或点击 Web 按钮的瞬间工作：导入文件 → 跑 pytest → 记录结果 → 备份/回滚。
> 所有 AI Agent 交互都由录制者手动完成；把 Agent 换成真人手写并交付同样的文件，Harness
> 的流程与判定完全不变。Harness 是流程导演台、裁判台和记分牌，不是 Agent 编排器。

## 1. 仓库定位

| 仓库 | 说明 |
| ---- | ---- |
| `harness_repo`（本仓库） | 评测框架：FastAPI Web 控制台 + CLI + 裁判核心 |
| `arena_repo`（人类另建） | 真正被选手 AI 修改的业务代码仓库，**独立 git 仓库**，结构为 `src/`（业务代码，可由 `business_dir` 配置）+ `tests/`（历史 pytest 测试，一旦存在即锁定，禁止修改/删除） |

框架通过 `config.json` 的 `arena_repo_path`（或环境变量 `BUGRELAY_ARENA_REPO`，见 §2）指向 arena_repo（默认 `../arena_repo`，即两个仓库并排放）。
**arena_repo 不存在时，框架/CLI/Web 仍能正常启动**，只是相关操作显示"arena_repo 未就绪"，不会抛异常崩溃。

## 2. 安装与部署

需要交给 Hermes Agent 在专用 Ubuntu VM 上部署时，直接使用完整任务书：
[HERMES_DEPLOYMENT.md](HERMES_DEPLOYMENT.md)。

依赖：fastapi、uvicorn、pytest、python-multipart（FastAPI 文件上传必需），可选
python-dotenv、rich（CLI 美化）。需要 **Python ≥ 3.10**。Harness 不调用任何模型 API。

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

推荐把 Harness 和 arena 放在一台专门用于录制/评测的 Ubuntu 虚拟机中，但它们必须是
**两个互相独立的 Git 仓库**。`BR-Code` 只是一个可供实战使用的 arena 示例，不是 Harness
的内置子目录或运行时依赖。

### 指定 arena_repo 路径（三种方式，优先级从高到低）

1. **环境变量**（换机器不改任何文件，适合脚本/systemd）：`export BUGRELAY_ARENA_REPO=/home/me/repos/arena_repo`
2. **config.json 的 `arena_repo_path`**：支持绝对路径、`~` 开头、或相对路径（相对本仓库根解析）
3. **默认 `../arena_repo`**：把 harness 与 arena_repo 两个仓库**并排克隆**即可直接用：

   ```
   repos/ $ git clone https://github.com/Vita0818/BugRelay-Harness.git
   repos/ $ git clone <你的 arena_repo.git>     # 与 harness 平级
   ```

路径无效或未就绪时页面只提示不崩溃；指向 harness 自身或其祖先/子目录会被安全策略拒绝。

推荐的 Ubuntu 虚拟机目录布局：

```text
/home/recorder/bugrelay/
├── BR-Harness/   # 本仓库：网页、状态、备份、测试裁判
└── BR-Code/      # 独立 arena Git 仓库；本次实战恰好使用它
```

启动前显式指向本次 arena：

```bash
cd /home/recorder/bugrelay/BR-Harness
export BUGRELAY_ARENA_REPO=/home/recorder/bugrelay/BR-Code
./run.sh
```

以后换另一套题，只需把环境变量改到另一个独立 arena；Harness 不依赖 `BR-Code` 这个名字。

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

选手轮流接力，每轮四步。选手名单在 `state/match.json`：

- `order`：接力顺序，使用**三字码**（如 `FBL`、`OPS`），日志与状态均用三字码标识；
- `players`：三字码 → `{ "name": 总称, "model": 实际模型 }` 的映射，控制台选手席与 CLI
  显示总称，悬停/括号内显示实际模型。模型迭代时只改 `model` 字段，三字码与历史记录不变。

**模型迭代（部署后不改文件）**：控制台选手席右上角「编辑模型」进入批量编辑（回车保存、
Esc 取消），或 CLI `python -m cli.bugrelay set-model <三字码> "<新实际模型名>"`，
或 `POST /api/set-model`（`{"updates": {"FBL": "..."}}`，支持一次改多个）。三种入口
只改 `players.<三字码>.model`，其余一概不动，并写入 `set-model` 日志留痕。

当前阵容 32 位（Claude 系 / GPT 系 / Gemini 系 / Grok / Muse 系 / Nemotron / Mistral /
DeepSeek 系 / GLM 系 / Kimi / MiniMax / Qwen 系 / Hunyuan / Seed 系 / LongCat / StepFun /
Mimo 系），增删选手只需同步改 `order`、`survivors`、`players`、`scores` 四处。

1. **答题**：当前选手按上一棒留下的 `next_prompt.md`（仅需求，不含测试）修改 arena_repo 业务代码。
   人类导入其改动后点「验收答题」→ 框架备份 → 只把上传内容应用到 `src/`（严禁触碰 `tests/`，
   应用前后及 pytest 结束后用内容哈希清单保护 `tests/`，检测到篡改立即回滚并判 FAIL）→
   全绿：隐藏测试永久移入 `tests/` 成为历史测试、代码保留、再备份；否则：回滚、选手淘汰、切换下一位。
   **答题环节不调用任何模型。**
2. **出题**：答题通过后，选手提交给下一棒的需求文档 `next_prompt.md` + 三条新测试
   `hidden_tests.py`。框架先在一次性副本上运行它们：历史测试必须继续全绿，新测试必须
   实际收集三条并全部断言失败（全 RED）；零收集、skip、xfail、import/collection error
   都不算有效 RED。
3. **人工自证**：全红通过后，框架创建持久目录 `tmp/manual_proof_*/repo`，内容是当前正式
   arena 的干净副本，**不含新隐藏测试**。网页进入第 ④ 阶段并暂停。录制者手动在该目录
   打开全新的同模型 OpenCode（也可以由真人编码），把同目录旁的 `next_prompt.md` 粘贴进去。
   Harness 不启动、不调用、不监控这个 Agent。
4. **全绿与交棒**：人工自证完成后再次点 `NEXT`。框架先确认只有 `business_dir` 被修改，
   再复制自证结果、注入同一份新测试并运行 pytest；历史 + 新测试必须全部 GREEN、无
   skip/error。通过后只保存需求与隐藏测试并交棒，**自证代码丢弃，不写回正式 arena**；
   失败则出题者淘汰，但其此前已通过答题验收的正式代码继续保留。

规则：历史测试永增不减；淘汰只取消比赛资格，此前成功提交的代码留在 arena_repo 中成为后人的环境。

## 5. 操作流程

### 5.1 首轮（人类准备）

1. 人类另建独立 git 仓库 `arena_repo`（与 harness_repo 平级），含 `src/`（业务代码）与 `tests/`（初始历史测试，可为空目录）。**历史测试一旦存在即锁定。**
2. 导入**首轮需求**（给第一位选手的提示词，开局一次性）：调试抽屉「当前需求」块上传
   （或 CLI `python -m cli.bugrelay set-first-prompt <md>`、`POST /api/first-prompt`）。
   框架把它存为 `prompts/round_1_initial.md` 并指向它；仅当当前没有任何需求时允许设置。
   首轮隐藏测试 `hidden_tests.py` 放入 `hidden_tests/`（同样须遵守「测试规范」，见第 9 节——
   首轮是历史测试之母，更要干净自包含）。
   > 选手只能看到需求文档（调试抽屉「当前需求」完整展示，可一键复制喂给选手 Agent），
   > **永远看不到 hidden_tests/ 的任何内容**。
3. **顺序抽签（每场开始）**：点控制台顶部「抽签顺序 / Draw」按钮（或 CLI `draw`、`POST /api/draw`），
   为全部选手随机抽出接力顺序（`SystemRandom` 公平洗牌）。抽签会重置比赛进度（轮次=1、
   积分清零、无淘汰），但**保留** `players` 选手表与已设置的首轮需求；之后接力按抽中顺序
   从 1 号位循环到最后一位再回到 1 号，一直接力下去。选手席卡片上的小号数字即接力位次。

### 5.2 每轮循环（每位选手固定四步：作答 → 判定 → 出题 → 自证）

控制台主画面是**横向 1×4 阶段带**，编号即时间顺序 ① 作答 → ② 判定 → ③ 出题 → ④ 自证，
从左到右接力，实时显示当前选手所处步骤（黑底=当前/评测中、描边打勾=本轮已完成）；
顶栏与底栏占据更多画面（赛况横幅 / 判定结果常驻面板）。**人类只需一个 `NEXT`**：
答题阶段运行全绿判定；出题材料导入后第一次运行全红判定；全红通过后页面停在第 ④ 格，
等待录制者手动完成 OpenCode；再次 `NEXT` 才运行全绿并交棒。底栏常驻「判定结果」面板
（总判定 PASS/FAIL + 历史/隐藏计数 + 一句结果
说明），判定瞬间全屏定格 3 秒——**整屏绿色 PASS / 整屏红色 RED 或 FAIL**（录屏高潮镜头，
点击可提前关闭）。所有操作按钮点击即执行，无二次确认弹窗。

| 步骤 | 谁在做 | 发生什么 |
| ---- | ---- | ---- |
| ① 作答 | 当前选手 | 按需求改 arena_repo 业务代码，交付物落 `inbox/`（或人工上传） |
| ② 判定 | 框架（人类点「推进」，答题阶段自动执行验收） | 应用 ① 的改动到 `src/`，跑历史+隐藏 pytest：全绿过；否则淘汰回滚 |
| ③ 出题 | 当前选手 + 框架 | 交下一棒需求与 3 条新测试；框架要求历史全绿、新测试全部 RED |
| ④ 自证 | 录制者手动操作 + 框架判定 | 在独立目录手动启动全新的同模型 OpenCode（或真人编码）；完成后再按 `NEXT`，历史+新测试全部 GREEN 才交棒 |

```
① 作答 → ② 判定 → ③ 出题 → ④ 自证
              │FAIL: 淘汰+回滚，换下一位          │FAIL: 出题者淘汰，换下一位
              ↓                                  ↓
        （下一位回到 ①）                   （下一位回到 ①；全绿则轮次+1 交棒）
```

Web 操作区只有一个 `NEXT` 主按钮，复用既有四阶段结构，不在页面内编排 Agent。全红通过后，
现有结果消息显示自证目录；现有提示词区域切换为待自证的 `next_prompt.md`，可直接复制到
手动打开的 OpenCode。CLI `status` 同样显示自证目录与提示词路径。

### 5.3 材料投递目录 inbox/（免手动上传，推荐）

**框架收材料本来就是文件级的**（Web 上传文件 / CLI 指路径），无需复制粘贴内容。inbox/
更进一步：把"挑文件"也省掉——Agent 按约定文件名交付，你只按一个按钮：

| 材料 | inbox/ 约定 | 触发方式 |
| ---- | ---- | ---- |
| 答题（业务代码改动） | `inbox/answer.zip` 或 `inbox/answer/` 目录 | 点「验收答题」（或 `judge-answer`），尚无已导入材料时自动拾取 |
| 出题（下一棒需求 + 隐藏测试） | `inbox/next_prompt.md` + `inbox/hidden_tests.py` | 点 `NEXT`（或第一次 `judge-proposal`）自动拾取并运行全红 |

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
python -m cli.bugrelay judge-proposal                  # 第一次：全红并创建手动自证目录
python -m cli.bugrelay status                          # 查看自证目录；手动在其中完成 OpenCode
python -m cli.bugrelay judge-proposal                  # 第二次：全绿并交棒
python -m cli.bugrelay restore                         # 还原最近备份
python -m cli.bugrelay set-model FBL "Claude Fable 5.5" # 模型迭代：更新选手实际模型
python -m cli.bugrelay draw                            # 顺序抽签：随机重排接力顺序并重置进度（每场开始时）
python -m cli.bugrelay inject-rules                    # 手动注入测试规范到 arena_repo（平时自动注入并自愈）
python -m cli.bugrelay web                             # 启动 Web
```

> CLI/Web 没有任何“自动跑 Agent”的能力；AI 与真人都是框架外的文件产出者。

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
| POST | `/api/judge-proposal` | 状态化推进：第一次全红并创建自证目录；第二次全绿并交棒 |
| POST | `/api/restore` | 还原最近备份 |
| POST | `/api/set-model` | 批量更新选手实际模型 `{"updates": {三字码: 新模型名}}`（模型迭代；arena 未就绪也可用） |
| POST | `/api/draw` | 顺序抽签：随机重排全部选手接力顺序并重置比赛进度（保留选手表与首轮需求） |
| POST | `/api/inject-rules` | 手动把测试规范写入 arena_repo/TESTING_GUIDELINES.md（幂等；平时自动注入自愈） |

安全约定：**前端任何接口都不返回 hidden_tests/ 内容**；测试结果只显示总结果与
通过数/总数，测试函数名、断言内容、diff 一律不展示（pytest 原始输出只留在服务端终端供人类排障）。

## 7. 配置（config.json）

| 键 | 说明 |
| ---- | ---- |
| `arena_repo_path` | arena_repo 位置（默认 `../arena_repo`；支持绝对路径、`~`、相对本仓库的路径）。可被环境变量 `BUGRELAY_ARENA_REPO` 覆盖（优先级更高） |
| `business_dir` / `history_tests_dir` | 业务目录（默认 `src`）与历史测试目录（默认 `tests`） |
| `pytest_args` | 传给 pytest 的参数（默认 `["-q"]`，框架会追加 junitxml/legacy 等解析用参数） |
| `proposal_test_count` | 每道新题必须包含且被 pytest 实际收集的测试条数（默认 3） |
| `state_file` / `hidden_tests_dir` / `prompts_dir` / `backups_dir` | 框架自身目录布局 |
| `inbox_dir` | 材料投递目录（默认 `inbox`，可指向 Agent 的产物输出目录，见 §5.3） |
| `mock` | MOCK 演练模式开关（默认 `false`）。开启后随机模拟答题全绿、出题全红和自证全绿，不跑 pytest、不读写 arena_repo、不需要材料；仍保留“全红后暂停、第二次推进全绿”的真实节奏 |

## 7.1 MOCK 演练模式（上真实流程前预演整场操作）

用途：不接 arena_repo，把“抽签 → 作答判定 → 出题全红 → 手动自证暂停 → 自证全绿 → 交棒/淘汰”整条操作链走一遍。

- **开启**：Web 顶栏「MOCK」按钮（confirm 后生效）或 `POST /api/mock {"enabled": true}`；
- **判定随机**：每次点「验收答题 / 校验出题」随机出 PASS/FAIL（约 65% PASS），日志与返回值均带 `[MOCK]` 标记；
- **状态推进与真实一致**：出题第一次推进模拟全红；通过后暂停等待手动自证，第二次推进才模拟全绿并交棒；
- **不碰任何真实资源**：不运行 pytest、不读写 arena_repo、不要求 inbox/ 上传材料（arena 未就绪也可演练）；
- **演练需求占位**：交棒后 `current_prompt_file` 为 `mock://round_N` 标记，需求面板显示占位文本；
- **收尾**：演练完点「抽签」重置进度，再关 MOCK 回到真实评测（顶栏按钮 / `POST /api/mock {"enabled": false}` / 改 config.json 均可）。


## 8. 目录结构

```
harness_repo/
├── app.py                # FastAPI 应用
├── requirements.txt
├── config.json
├── cli/bugrelay.py       # argparse CLI
├── core/
│   ├── judge.py          # 裁判核心：pytest、备份、回滚、全红/全绿与手动自证状态
│   ├── repo_ops.py       # arena_repo 复制、测试内容清单、还原、上传应用
│   └── utils.py          # 配置/状态/日志
├── templates/index.html  # 单页控制台
├── static/               # style.css + main.js
├── hidden_tests/         # 暂存本轮隐藏测试（评测后清理/归档进 arena tests/）
├── backups/              # arena_repo 备份（index.json 登记，会增长，可手动清理旧目录及登记项）
├── prompts/              # 每轮合法的 next_prompt.md
├── inbox/                # 材料投递目录（Agent 交付物落点，自动拾取，见 §5.3）
└── state/                # match.json + log.jsonl（最新在上）
```

`tmp/` 为运行时目录：上传暂存、一次性 RED/GREEN 测试副本，以及全红通过后需要人工
进入的 `manual_proof_*/repo`。后者会跨请求保留到全绿判定结束；不要把新隐藏测试手动
复制进去。

## 9. 测试规范（出题契约）

出题人（AI Agent 或真人）以及首轮人类提交的 `hidden_tests.py` 必须满足以下规范。

**规范的注入**：arena_repo 接入框架（`arena_ready` 刷新为就绪）的那一刻，本规范会自动写入
`arena_repo/TESTING_GUIDELINES.md`——选手 Agent 打开仓库即可看到（面向选手改写的版本，
含答题规则/出题规则/数据内联示例/交付物文件名约定）；文件被删后下次刷新自动补回
（自愈）。需要立即重写：Web「重注入规范 / Rules」按钮、CLI `inject-rules`、
`POST /api/inject-rules`（均幂等）。

违反阻断级规则的文件在导入时（Web 上传 / CLI load-proposal / inbox 拾取）会被静态闸门
拒绝，不会创建自证目录。**建议把本节原文（或注入的 TESTING_GUIDELINES.md）
直接粘进给出题选手的提示词**。

### 9.1 hidden_tests.py 写法（阻断级，机器校验）

1. **单文件自包含**：一个 `hidden_tests.py` 就是全部。禁止引用 `tests/` 内其他模块
   （`import tests...` / `from tests...`）、禁止相对导入（`from . / from ..`）——
   验题时它会被**单独拷贝**到临时仓库，任何外部依赖都会挂。
2. **三个模块顶层普通函数**：禁止嵌套、测试类方法、async、参数化、fixture 参数、
   `@pytest.fixture`、skip 和 xfail；每个 `test_*` 必须无参数，数据在函数体内构造。
3. **必须有真测试**：每个 `test_*` 函数都含 `assert` 或 `pytest.raises`；缺失直接拒绝。
4. **语法必须合法**（废话，但 AI 常翻车）。
5. **一题一缺陷、三测同源**：每次出题只引入**一个**缺陷（需求变更）；
   `hidden_tests.py` 必须恰好包含 **3 个** `test_*` 函数（数量可经
   `config.json` 的 `proposal_test_count` 调整，默认 3），全部针对同一个缺陷，
   从不同角度夹住它——推荐分工：主路径复现 / 边界邻近值 / 回归防护（防特判
   糊弄）。禁止在一个文件里测多个互不相关问题（引用多个业务模块会被警告）。

### 9.2 稳定性要求（强烈建议，无法静态校验）

- **确定性**：同一份代码跑两遍结果必须一致——不用随机（必须用就固定 seed）、
  不依赖当前时间/时区、不依赖字典遍历顺序。
- **无外部依赖**：不访问网络、不读写仓库外文件、不启动进程/端口。
- **限时**：全部测试须在 pytest 超时预算内跑完（框架设了全局超时，超时直接判 FAIL），
  单个用例建议 < 1 秒。
- **彼此独立**：不依赖执行顺序，不共享可变全局状态。

### 9.3 导入被测代码（唯一正确姿势）

框架把 arena_repo 根目录加入 `PYTHONPATH` 后在**仓库根目录**运行 pytest。业务代码在
`src/`（`business_dir` 可配），因此导入一律用包路径前缀：

```python
from src.kv import KVStore          # src/kv.py
from src.storage.json_db import DB  # src/storage/json_db.py
```

不要 `import kv`（找不到）、不要 `sys.path.insert(...)`（画蛇添足）、不要修改
`conftest.py`/`pytest.ini`（tests/ 锁定，改了判篡改）。

### 9.4 测试数据从哪来（"去哪里找数据"）

**全部内联在测试文件里**——这是本赛制的硬约束：hidden_tests.py 是单文件交付物，
不能附带数据文件；arena_repo 也不预置业务数据（业务代码本身就是各选手写的）。

```python
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
    assert kv.get("x") == 1 and kv.get("y") == 2
    assert kv.get("不存在") is None
```

出题思路：**用"构造数据 + 断言行为"代替"加载现成数据"**；三个用例从三个角度
夹住**同一个**缺陷。边界值、空态、异常路径都是好题；纯正常路径的题太弱，
也无法形成清晰的“当前实现全红 → 独立自证全绿”。

### 9.5 框架执行规范（跑测试的机器行为）

| 项 | 规则 |
| --- | --- |
| 命令 | `<python> -m pytest <pytest_args> --junitxml=<tmp> -o junit_family=legacy --tb=short -p no:cacheprovider` |
| 工作目录 | 目标仓库根（arena 或临时验题仓库） |
| 发现范围 | pytest 默认规则：`test_*.py` / `*_test.py`；`tests/` 不存在时当作 0 个历史测试 |
| PYTHONPATH | 目标仓库根 → `from src...` 直接可用 |
| 隐藏测试注入 | 只注入一次性测试副本；手动自证目录在 Agent 工作期间不含新测试 |
| 全红判定 | 历史全绿；新测试恰好收集 N 条且全部 failure，passed/skipped/errors 均为 0 |
| 全绿判定 | 历史全绿；新测试恰好收集 N 条且全部 passed，failed/skipped/errors 均为 0 |
| 计数 | junit XML 严格区分 passed/failed/errors/skipped；前端只展示必要计数，不展示测试内容 |
| 超时 | 全局 pytest 超时（`PYTEST_TIMEOUT_SECONDS`），超时直接判 FAIL |
| 历史测试保护 | 应用前、应用后、pytest 后比较 `tests/` 内容哈希；任一历史文件变化即回滚 |



## 10. 故障排查

- **arena_repo 不存在**：页面顶部与文件树区会提示"arena_repo 未就绪"；确认 `config.json`
  的 `arena_repo_path`（或环境变量 `BUGRELAY_ARENA_REPO`）指向已存在的独立 git 仓库后点「刷新」。路径非法（指向 harness 自身或其
  祖先/子目录）也会被判为未就绪，属安全防护。
- **全红通过后怎么办**：页面停在第 ④ 格；结果消息和 `bugrelay status` 给出
  `tmp/manual_proof_*/repo`。在该目录手动打开全新的同模型 OpenCode，复制页面现有提示词，
  完成后再点 `NEXT`（或再次运行 `judge-proposal`）做全绿判定。
- **pytest 路径问题**：框架在仓库根目录以 `python -m pytest` 运行并把仓库根加入
  `PYTHONPATH`；若历史测试仍找不到被测代码，检查 arena_repo 的测试导入方式与
  `pytest_args` 配置。
- **首轮验收报"本轮隐藏测试缺失"**：需人工把该轮 `hidden_tests.py` 放入 `hidden_tests/`。
  另注：若某选手出题失败被淘汰，本轮新需求被拒收，下一棒将面对旧需求继续（此时
  `hidden_tests/` 可能为空，同样需人工补充后再验收）。
- **backups/ 增长**：每次验收都会全量备份 arena_repo；可手动删除旧备份目录并同步
  从 `backups/index.json` 移除对应登记项。

## 11. 边界声明

- 本框架仓库**不包含任何被评测的业务代码**，也不生成 demo 业务数据；
- Harness 与 arena 必须是两个独立 Git 仓库；它们可以并排放在专用 Ubuntu 虚拟机中，
  `BR-Code` 只是一次实战选择，不是 Harness 的组成部分；
- 框架不调用 AI；AI Agent 或真人都由录制者手动操作，只向 Harness 交付文件；
- 框架**不创建** arena_repo（由人类另建），只在人类触发评测时对既有仓库做
  受控的备份/应用/回滚；
- 历史测试一经进入 arena_repo 的 `tests/` 即锁定：上传内容顶层 `tests/` 目录会被
  整体拦截，内容哈希清单发现改动立即回滚并判 FAIL。
