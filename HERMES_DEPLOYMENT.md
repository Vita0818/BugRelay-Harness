# Hermes Agent 部署说明：Ubuntu VM 上的 Bug Relay

本文是交给 Hermes Agent 的完整部署任务书。部署目标是：在一台专用 Ubuntu 虚拟机中运行
`BR-Harness`，并把一个**独立 Git 仓库**作为 arena；当前实战 arena 恰好是 `BR-Code`。

Harness 只是网页流程控制台、pytest 裁判、状态/备份管理器。它不启动、不调用、不监控
任何 AI Agent。所有 OpenCode/AI/真人编码都由录制者在框架外手动完成。

## 1. 不可违反的约束

Hermes Agent 必须遵守以下约束：

1. `BR-Harness` 与 `BR-Code` 必须是两个互相独立的 Git 仓库；两者可以并排，但不能嵌套。
2. 不得把 `BR-Code` 复制进 `BR-Harness`，也不得把 Harness 复制进 Code。
3. 部署期间不得修改 `BR-Code/src/` 和 `BR-Code/tests/`。Harness 启动后在 Code 根目录
   注入或更新 `TESTING_GUIDELINES.md` 属于预期行为。
4. 不得恢复或重新加入 `verifier_model`、OpenAI API、Ollama、`httpx` 或任何自动 Agent 调用。
5. 不得修改网页主体结构、CSS、阶段布局或增加新 UI 元素。部署任务只做环境配置和启动。
6. 只运行一个 Uvicorn 进程，不得使用 `--workers`，不得创建多实例负载均衡。
7. Harness 服务应使用与录制者手动运行 OpenCode 相同的 Linux 用户；否则手动自证目录
   `BR-Harness/tmp/manual_proof_*/repo` 可能出现写权限问题。
8. 不得在部署时执行 Draw、开始比赛、导入首轮提示词或生成隐藏测试。这些属于录制流程。
9. 不得执行 `git reset --hard`、`git clean -fdx`、删除 `state/`、`backups/`、`prompts/`、
   `hidden_tests/` 或 `inbox/`。若目录已有内容，先备份并报告。
10. 不得猜测仓库 URL、Linux 用户名、操作员主机 IP 或已有目录的处理方式；缺少这些输入时
    应暂停并向操作者索取，而不是自行替换已有数据。

## 2. Hermes 必须先获得的输入

部署前确认以下值。文档中的尖括号必须替换为真实值，不得原样写入 systemd：

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `DEPLOY_USER` | `recorder` | 运行 Harness 和手动 OpenCode 的 Ubuntu 用户 |
| `INSTALL_ROOT` | `/home/recorder/bugrelay` | 两个独立仓库的共同父目录 |
| `HARNESS_REPO_URL` | `<由操作者提供>` | BR-Harness Git URL |
| `ARENA_REPO_URL` | `<由操作者提供>` | 本次 BR-Code Git URL |
| `LISTEN_HOST` | `0.0.0.0` | 宿主机浏览器要访问 VM 时使用；仅 VM 内浏览则用 `127.0.0.1` |
| `LISTEN_PORT` | `8080` | Web 控制台端口 |
| `OPERATOR_HOST_IP` | `<由操作者提供>` | 仅在需要配置 VM 防火墙时使用 |

本文后续假定：

```bash
DEPLOY_USER=recorder
INSTALL_ROOT=/home/recorder/bugrelay
HARNESS_DIR=/home/recorder/bugrelay/BR-Harness
ARENA_DIR=/home/recorder/bugrelay/BR-Code
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8080
```

Hermes 执行时应使用真实绝对路径，不要依赖未导出的 shell 临时变量。

## 3. 目标目录布局

最终必须是：

```text
/home/recorder/bugrelay/
├── BR-Harness/
│   ├── .git/
│   ├── app.py
│   ├── config.json
│   ├── core/
│   ├── state/
│   ├── backups/
│   ├── prompts/
│   ├── hidden_tests/
│   ├── inbox/
│   └── tmp/
└── BR-Code/
    ├── .git/
    ├── src/
    ├── tests/
    └── ...
```

部署前做只读校验：

```bash
test -d /home/recorder/bugrelay/BR-Harness/.git
test -d /home/recorder/bugrelay/BR-Code/.git
test -d /home/recorder/bugrelay/BR-Code/src
test -d /home/recorder/bugrelay/BR-Code/tests
git -C /home/recorder/bugrelay/BR-Harness rev-parse --show-toplevel
git -C /home/recorder/bugrelay/BR-Code rev-parse --show-toplevel
```

两个 `rev-parse --show-toplevel` 必须返回两个不同目录。arena 不得是 Harness 的父目录、
子目录或自身。

## 4. 安装系统依赖

适用于 Ubuntu 22.04/24.04。项目需要 Python 3.10 或更高版本。

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip curl jq
python3 --version
```

若 Python 低于 3.10，停止部署并安装受支持版本，不要修改源码去兼容旧解释器。

## 5. 获取或复用两个仓库

### 5.1 目录尚不存在

使用操作者提供的 URL：

```bash
mkdir -p /home/recorder/bugrelay
git clone <HARNESS_REPO_URL> /home/recorder/bugrelay/BR-Harness
git clone <ARENA_REPO_URL> /home/recorder/bugrelay/BR-Code
```

### 5.2 目录已经存在

先只读检查，不得重置或清理：

```bash
git -C /home/recorder/bugrelay/BR-Harness status --short --branch
git -C /home/recorder/bugrelay/BR-Code status --short --branch
```

若有未提交改动：

- 不得执行 reset/checkout/clean 覆盖它们；
- 报告具体文件；
- 等待操作者决定继续使用、提交、备份还是重新部署到其他目录。

只有工作树处理策略得到操作者确认后，才可以执行 `git pull --ff-only`。不得自行 merge/rebase。

## 6. 创建 Python 虚拟环境并安装依赖

在 Harness 仓库执行：

```bash
cd /home/recorder/bugrelay/BR-Harness
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

不要在 Code 仓库创建 Harness 的虚拟环境；不要把 `.venv` 放到两个仓库的共同父目录。

## 7. 配置 arena 和真实模式

### 7.1 使用环境文件指定独立 arena

创建仅供 systemd 使用的环境文件：

```bash
sudo install -d -m 0755 /etc/bugrelay
sudo tee /etc/bugrelay/bugrelay.env >/dev/null <<'EOF'
BUGRELAY_ARENA_REPO=/home/recorder/bugrelay/BR-Code
PYTHONUNBUFFERED=1
EOF
sudo chmod 0644 /etc/bugrelay/bugrelay.env
```

`BUGRELAY_ARENA_REPO` 优先于 `config.json` 的 `arena_repo_path`。不要把 Code 改成 Harness
子目录，也不要为了本次实战把后端源码硬编码为 `BR-Code`。

### 7.2 关闭 MOCK

把 `/home/recorder/bugrelay/BR-Harness/config.json` 的顶层字段设置为：

```json
"mock": false
```

保留其他字段，不要重建整个配置文件。确认配置中不存在：

```text
verifier_model
base_url
api_key
chat/completions
```

校验 JSON：

```bash
jq empty /home/recorder/bugrelay/BR-Harness/config.json
jq '.mock' /home/recorder/bugrelay/BR-Harness/config.json
```

第二条命令必须输出 `false`。

### 7.3 权限

Harness 需要写入自身运行时目录和 arena。确保服务用户与手动 OpenCode 用户一致：

```bash
sudo chown -R recorder:recorder /home/recorder/bugrelay/BR-Harness
sudo chown -R recorder:recorder /home/recorder/bugrelay/BR-Code
```

不要在不知道真实用户和组的情况下原样执行；必须把 `recorder` 替换为 `DEPLOY_USER`。

## 8. 部署为单进程 systemd 服务

创建 `/etc/systemd/system/bugrelay.service`。必须替换所有示例用户名和路径：

```ini
[Unit]
Description=Bug Relay Harness
After=network.target

[Service]
Type=simple
User=recorder
Group=recorder
WorkingDirectory=/home/recorder/bugrelay/BR-Harness
EnvironmentFile=/etc/bugrelay/bugrelay.env
ExecStart=/home/recorder/bugrelay/BR-Harness/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=2
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

重要：

- `ExecStart` 中不得出现 `--workers`；Uvicorn 默认单进程。
- 不要同时再用 `./run.sh` 或另一条 Uvicorn 命令启动第二个实例。
- `User` 必须是之后手动在自证目录运行 OpenCode 的同一用户。

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bugrelay.service
sudo systemctl status bugrelay.service --no-pager
```

查看日志：

```bash
journalctl -u bugrelay.service -n 100 --no-pager
journalctl -u bugrelay.service -f
```

## 9. 网络访问

若浏览器就在 VM 中，优先绑定 `127.0.0.1`。若从宿主机访问 VM，可绑定 `0.0.0.0`，但
只应通过 NAT/host-only 网络或防火墙开放给操作者主机。

若 Ubuntu 使用 UFW，并且已获得真实 `OPERATOR_HOST_IP`：

```bash
sudo ufw allow from <OPERATOR_HOST_IP> to any port 8080 proto tcp
```

不得用猜测的 IP 创建规则。不要把 8080 端口暴露到公网。

VM 内健康检查：

```bash
curl -fsS http://127.0.0.1:8080/api/state | jq .
```

宿主机浏览器地址：

```text
http://<VM_IP>:8080/
```

## 10. 部署前自动验证

在不启动比赛、不抽签的前提下执行：

```bash
cd /home/recorder/bugrelay/BR-Harness
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q --tb=short -p no:cacheprovider
jq empty config.json
curl -fsS http://127.0.0.1:8080/api/state | jq '{ok, mock, step, arena_ready: .state.arena_ready}'
```

预期：

```text
10 passed
ok = true
mock = false
arena_ready = true
```

`/api/state` 的启动刷新可能在 BR-Code 根目录创建或更新 `TESTING_GUIDELINES.md`；这是
Harness 的规范注入机制，不是对业务代码或历史测试的修改。

再次确认 Code 的业务与测试没有被部署步骤改动：

```bash
git -C /home/recorder/bugrelay/BR-Code status --short -- src tests
```

如果部署前 `src/tests` 是干净的，这里也必须无输出。

可选：如果 VM 已安装 Node.js，再执行：

```bash
node --check /home/recorder/bugrelay/BR-Harness/static/main.js
```

不要只为了这项可选检查额外安装 Node.js。

## 11. 部署完成后不要替操作者执行的事项

Hermes 部署到此为止。以下事项必须留给录制者本人：

- 不要点击或调用 Draw；
- 不要清空当前 MOCK 演练 state；
- 不要上传首轮提示词；
- 不要创建或导入首轮隐藏测试；
- 不要放置答题/出题材料到 `inbox/`；
- 不要运行 `judge-answer` 或 `judge-proposal`；
- 不要自动启动任何 OpenCode/Agent；
- 不要进入 `tmp/manual_proof_*/repo` 修改代码。

录制者开始正式比赛时会自行：

1. 给 VM 做快照；
2. 在页面确认 MOCK 已关闭、arena 已就绪；
3. 在镜头中执行 Draw，清除旧 MOCK 轮次并产生正式顺序；
4. 导入首轮提示词和首轮隐藏测试；
5. 手动操作各选手 Agent；
6. 使用页面 `NEXT` 推进 GREEN、RED、手动自证、GREEN 与交棒。

## 12. 手动自证阶段的运维事实

全红通过后，Harness 会在页面结果消息、当前提示词区域和 CLI `status` 中给出：

```text
自证目录：BR-Harness/tmp/manual_proof_r.../repo
提示词：  BR-Harness/tmp/manual_proof_r.../next_prompt.md
```

录制者会以服务用户身份手动：

```bash
cd /home/recorder/bugrelay/BR-Harness/tmp/manual_proof_r.../repo
opencode
```

新隐藏测试不会出现在该 `repo` 中。人工自证结束后再次点击 `NEXT`，Harness 才会复制
自证结果、注入同一份隐藏测试并运行严格 GREEN。成功后自证目录被删除，自证代码不会
写回正式 BR-Code。

## 13. 更新部署

更新 Harness 前：

1. 确认当前没有 pytest 正在运行，也不处于 `pending_proof` 手动自证阶段；
2. 停止服务；
3. 备份运行时状态；
4. 检查工作树；
5. 只有操作者允许时才 fast-forward 更新。

示例：

```bash
sudo systemctl stop bugrelay.service
mkdir -p /home/recorder/bugrelay/deploy-backup
tar -C /home/recorder/bugrelay/BR-Harness -czf \
  /home/recorder/bugrelay/deploy-backup/harness-runtime-$(date +%Y%m%d_%H%M%S).tar.gz \
  config.json state backups prompts hidden_tests inbox
git -C /home/recorder/bugrelay/BR-Harness status --short --branch
```

若有未提交源码改动，停止并报告，不得 reset。得到许可后：

```bash
git -C /home/recorder/bugrelay/BR-Harness pull --ff-only
cd /home/recorder/bugrelay/BR-Harness
.venv/bin/python -m pip install -r requirements.txt
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m pytest -q --tb=short -p no:cacheprovider
sudo systemctl start bugrelay.service
curl -fsS http://127.0.0.1:8080/api/state | jq .
```

不要自动 pull 或更新 BR-Code；arena 的版本由录制者单独管理。

## 14. Hermes 最终交付报告

Hermes 完成部署后，应向操作者报告且仅报告事实：

1. Ubuntu 与 Python 版本；
2. `DEPLOY_USER`；
3. Harness 和 Code 的绝对路径；
4. 两个 Git 仓库各自的 commit 和 `git status --short --branch`；
5. `BUGRELAY_ARENA_REPO` 的最终值；
6. `mock` 的最终值；
7. Harness pytest 结果；
8. systemd 服务状态；
9. VM 内健康检查结果；
10. 浏览器访问 URL；
11. UFW/NAT 设置（若有）；
12. 是否创建/更新了 BR-Code 根目录的 `TESTING_GUIDELINES.md`；
13. 明确确认 `BR-Code/src/` 与 `BR-Code/tests/` 未被部署步骤修改；
14. 任何未解决警告或需要操作者决定的事项。

满足以下条件才算部署完成：

```text
Harness 测试全绿
systemd 单进程运行
mock=false
arena_ready=true
Harness/Code 为独立仓库
BR-Code/src 与 BR-Code/tests 未被部署修改
未启动任何 AI Agent
未执行 Draw 或正式比赛步骤
```
