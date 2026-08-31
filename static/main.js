/* Bug Relay 控制台前端逻辑（原生 JS，无依赖）
 *
 * 职责：
 * - 轮询 /api/state、/api/log（3 秒）渲染赛况与实时日志；
 * - 渲染 arena_repo 文件树（GET /api/tree，后端已过滤 hidden_tests/），
 *   点击文件查看内容（GET /api/file，前端渲染行号）；
 * - 展示当前需求（GET /api/prompt）与底部两行测试计数（只显示总结果与计数）；
 * - 人类点击按钮触发操作：上传答题/出题材料、验收、校验、还原。
 *
 * 安全约定：后端不会返回 hidden_tests/ 内容与测试细节，前端也不展示。
 */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  arenaReady: false,
  phase: null,
  mock: false,          // MOCK 演练模式：判定随机模拟（后端 /api/mock 控制）
  selectedFile: null,
  pendingAnswer: null,
  pendingProposal: null,
  runningStep: null,  // 评测运行中的步骤（2=判定，4=自证），本地状态
  lastState: null,      // 最近一次 /api/state 返回的 state（进入模型编辑时用）
  editingModels: false, // 模型编辑模式：轮询期间不重绘选手席，避免 3 秒清空输入
  modelOrig: {},        // 编辑模式的原始模型名 {三字码: model}，保存时求差集
  bannerHoldUntil: 0,   // 临时结果横幅（抽签/还原）的保持截止时间，超时后由轮询清除
};

/* ---------------- 通用请求 ---------------- */

async function api(url, options = {}) {
  try {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
    return data;
  } catch (e) {
    return { ok: false, error: "网络错误: " + e.message };
  }
}

function setMsg(el, text, cls) {
  el.textContent = text;
  el.className = "op-msg" + (cls ? " " + cls : "");
}

function showBanner(text, cls) {
  const b = $("banner");
  b.textContent = text;
  b.className = "banner " + cls;
}

function hideBanner() {
  $("banner").className = "banner hidden";
}

/* ---------------- 判定全屏定格（录屏高潮镜头） ---------------- */

let verdictTimer = null;

function showVerdict(word, detail, sub) {
  const v = $("verdict");
  $("verdict-label").textContent = state.mock ? "MOCK 演练 VERDICT" : "判定 VERDICT";
  $("verdict-word").textContent = word || "–";
  $("verdict-word").className = "verdict-word " + (word === "PASS" ? "pass" : "fail");
  $("verdict-detail").textContent = detail || "";
  $("verdict-sub").textContent = sub || "";
  v.classList.remove("hidden");
  if (verdictTimer) {
    clearTimeout(verdictTimer);
  }
  verdictTimer = setTimeout(hideVerdict, 2200); // 全屏定格约 2 秒
}

function hideVerdict() {
  $("verdict").classList.add("hidden");
}

/* ---------------- 赛况渲染 ---------------- */

function renderState(st, inbox, currentStep, mockOn) {
  state.arenaReady = !!st.arena_ready;
  state.phase = st.phase;
  state.mock = !!mockOn;
  state.lastState = st;
  const inboxAnswer = inbox && inbox.answer;
  const inboxProposal = inbox && inbox.proposal;

  // MOCK 徽章与开关按钮
  $("mock-badge").classList.toggle("is-hidden", !state.mock);
  const mockBtn = $("btn-mock");
  mockBtn.textContent = state.mock ? "MOCK 开" : "MOCK 关";
  mockBtn.classList.toggle("primary", state.mock);
  mockBtn.classList.toggle("ghost", !state.mock);

  $("st-round").textContent = st.round;
  // 当前选手：三字码 + 总称（悬停显示实际模型）
  const players = st.players || {};
  const cur = st.current_player;
  const curInfo = cur && players[cur];
  const playerEl = $("st-player");
  playerEl.textContent = cur ? (curInfo ? cur + " · " + curInfo.name : cur) : "–";
  if (curInfo && curInfo.model) {
    playerEl.title = curInfo.model;
  } else {
    playerEl.removeAttribute("title");
  }
  renderRoster(st);
  renderStepper(st, currentStep);

  // 存活/淘汰：人数为主（32 人名单太长），完整名单放悬停提示
  const surv = st.survivors || [];
  const elim = st.eliminated || [];
  const survEl = $("st-survivors");
  survEl.textContent = surv.length + " / " + (st.order || surv).length + " 人";
  survEl.title = surv.join(" · ");
  survEl.className = surv.length ? "ok" : "bad";
  const elimEl = $("st-eliminated");
  elimEl.textContent = elim.length ? elim.length + " · " + elim.join(" ") : "0";
  elimEl.className = elim.length ? "bad" : "";

  const arenaEl = $("st-arena");
  arenaEl.textContent = st.arena_ready ? "就绪" : "未就绪";
  arenaEl.className = st.arena_ready ? "ok" : "bad";

  const lastEl = $("st-last");
  lastEl.textContent = st.last_result || "–";
  lastEl.className = st.last_result === "PASS" ? "ok" : (st.last_result === "FAIL" ? "bad" : "");

  if (st.status === "finished") {
    showBanner("比赛已结束：无存活选手可继续接力。", "warn");
  } else if (Date.now() < (state.bannerHoldUntil || 0)) {
    // 抽签/还原等临时结果横幅保持 15 秒（32 位顺序需要阅读时间），优先于其他提示
  } else if (state.mock) {
    // MOCK 常驻提示交给顶栏徽章，不挂浮层横幅（避免遮挡赛况）
    hideBanner();
  } else if (!st.arena_ready) {
    showBanner("arena_repo 未就绪：请确认 config.json 的 arena_repo_path 指向已存在的独立 git 仓库（含 src/ 与 tests/）。在此之前仅能浏览页面，无法评测。", "warn");
  } else {
    hideBanner();
  }

  // 象征性需求提示：只示意"需求已给出"，不展示全文（全文在调试抽屉里）
  const ps = $("prompt-symbol");
  if (st.current_prompt_file) {
    ps.textContent = "需求已给出 next_prompt.md";
    ps.className = "prompt-symbol given";
  } else {
    ps.textContent = "等待首轮需求";
    ps.className = "prompt-symbol";
  }

  // 判定结果面板（常驻）：总判定 + 历史/隐藏两行计数 + 一句结果说明
  const sum = st.last_test_summary;
  const overall = $("t-overall");
  if (sum && sum.overall) {
    overall.textContent = sum.overall;
    overall.className = "badge big " + sum.overall;
  } else {
    overall.textContent = "–";
    overall.className = "badge big";
  }
  renderTestRow("t-history", sum && sum.history);
  renderTestRow("t-hidden", sum && sum.hidden);
  const rmsg = $("result-msg");
  rmsg.textContent = st.last_action_msg || "暂无判定";
  rmsg.className = "result-msg" +
    (st.last_result === "PASS" ? " ok" : (st.last_result === "FAIL" ? " err" : ""));

  // 按钮可用性（已导入材料 或 inbox/ 中检测到材料均可直接判定）
  // MOCK 演练模式：无需材料、无需 arena，按钮随阶段直接可用
  const finished = st.status === "finished";
  $("btn-judge-answer").disabled = !(
    (state.mock && st.phase === "answering" && !finished) ||
    (st.arena_ready && st.phase === "answering" && (st.pending_answer || inboxAnswer))
  );
  $("btn-judge-proposal").disabled = !(
    (state.mock && st.phase === "proposing" && !finished) ||
    (st.arena_ready && st.phase === "proposing" && (st.pending_proposal || inboxProposal))
  );
  $("btn-inject-rules").disabled = !st.arena_ready;
  $("btn-restore").disabled = !st.arena_ready;
  $("btn-upload-answer").disabled = !st.arena_ready;
  $("btn-upload-proposal").disabled = !st.arena_ready;

  state.pendingAnswer = st.pending_answer || null;
  state.pendingProposal = st.pending_proposal || null;
  if (st.pending_answer) {
    setMsg($("answer-msg"), "已导入待验收材料，可点击「验收答题」", "ok");
  } else if (inboxAnswer && st.phase === "answering" && st.arena_ready) {
    setMsg($("answer-msg"), "检测到 inbox/ 中有答题材料（answer.zip / answer/），点击「验收答题」将自动导入并评测", "ok");
  }
  if (st.pending_proposal) {
    setMsg($("proposal-msg"), "已导入待校验出题材料，可点击「校验出题并交棒」", "ok");
  } else if (inboxProposal && st.phase === "proposing" && st.arena_ready) {
    setMsg($("proposal-msg"), "检测到 inbox/ 中有出题材料（next_prompt.md + hidden_tests.py），点击「校验出题并交棒」将自动导入并验题", "ok");
  }
}

function renderTestRow(id, part) {
  const badge = $(id);
  const count = $(id + "-count");
  if (!part || !part.total) {
    badge.textContent = "–";
    badge.className = "badge";
    count.textContent = "";
    return;
  }
  const ok = part.passed >= part.total;
  badge.textContent = ok ? "PASS" : "FAIL";
  badge.className = "badge " + (ok ? "PASS" : "FAIL");
  count.textContent = "通过 " + part.passed + " / " + part.total;
}

/* ---------------- 文件树与文件预览 ---------------- */

async function loadTree() {
  const box = $("tree");
  const data = await api("/api/tree");
  box.innerHTML = "";
  if (!data.ok) {
    box.appendChild(el("div", { class: "empty" }, "加载失败: " + (data.error || "")));
    return;
  }
  if (!data.ready || !data.tree) {
    box.appendChild(el("div", { class: "empty" }, "arena_repo 未就绪"));
    return;
  }
  const ul = renderTreeNodes(data.tree.children || []);
  box.appendChild(ul);
  if (data.tree.truncated) {
    box.appendChild(el("div", { class: "empty" }, "（文件过多，已截断显示）"));
  }
}

function renderTreeNodes(nodes) {
  const ul = document.createElement("ul");
  for (const n of nodes) {
    const row = el("div", { class: "node-row" });
    const toggle = el("span", { class: "toggle" });
    if (n.type === "dir") {
      toggle.textContent = "▸";
      row.classList.add("node-dir");
      row.appendChild(toggle);
      row.appendChild(document.createTextNode(n.name + "/"));
      const childUl = renderTreeNodes(n.children || []);
      childUl.style.display = "none";
      row.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const open = childUl.style.display !== "none";
        childUl.style.display = open ? "none" : "block";
        toggle.textContent = open ? "▸" : "▾";
      });
      const li = document.createElement("li");
      li.appendChild(row);
      li.appendChild(childUl);
      ul.appendChild(li);
    } else {
      toggle.textContent = "·";
      row.appendChild(toggle);
      row.appendChild(document.createTextNode(n.name));
      row.addEventListener("click", () => {
        document.querySelectorAll(".tree .node-row.selected").forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        loadFile(n.path, n.name);
      });
      const li = document.createElement("li");
      li.appendChild(row);
      ul.appendChild(li);
    }
  }
  return ul;
}

async function loadFile(path, name) {
  const title = $("file-title");
  const view = $("fileview");
  title.textContent = path || name;
  view.textContent = "加载中…";
  const data = await api("/api/file?path=" + encodeURIComponent(path));
  view.textContent = "";
  if (!data.ok) {
    title.textContent = path + " — " + (data.error || "读取失败");
    return;
  }
  if (data.content == null) {
    view.textContent = data.message || "无法预览";
    return;
  }
  // 渲染带行号的内容（超过 3000 行截断）
  const lines = data.content.split("\n");
  const MAX = 3000;
  const frag = document.createDocumentFragment();
  const shown = Math.min(lines.length, MAX);
  for (let i = 0; i < shown; i++) {
    const lineDiv = document.createElement("div");
    lineDiv.className = "line";
    const ln = document.createElement("span");
    ln.className = "ln";
    ln.textContent = i + 1;
    const lc = document.createElement("span");
    lc.className = "lc";
    lc.textContent = lines[i] === "" ? " " : lines[i];
    lineDiv.appendChild(ln);
    lineDiv.appendChild(lc);
    frag.appendChild(lineDiv);
  }
  view.appendChild(frag);
  if (lines.length > MAX) {
    const tip = document.createElement("div");
    tip.className = "line";
    tip.textContent = "…（共 " + lines.length + " 行，仅显示前 " + MAX + " 行）";
    view.appendChild(tip);
  }
}

/* ---------------- 选手席 ---------------- */

function renderRoster(st) {
  const grid = $("roster-grid");
  if (!grid) { return; }
  if (state.editingModels) { return; } // 编辑中不重绘：3 秒轮询不能清空输入框
  grid.innerHTML = "";
  const players = st.players || {};
  const order = st.order && st.order.length ? st.order : (st.survivors || []);
  const survivors = st.survivors || [];
  const eliminated = st.eliminated || [];
  const scores = st.scores || {};
  order.forEach((code, i) => {
    const info = players[code] || {};
    const chip = el("div", { class: "chip" });
    if (code === st.current_player && st.status !== "finished") {
      chip.classList.add("current");
    } else if (eliminated.indexOf(code) !== -1) {
      chip.classList.add("out");
    }
    const head = el("div", { class: "chip-head" });
    head.appendChild(el("span", { class: "chip-pos" }, String(i + 1)));
    head.appendChild(el("span", { class: "chip-code" }, code));
    if (typeof scores[code] === "number") {
      head.appendChild(el("span", { class: "chip-score" }, String(scores[code])));
    }
    chip.appendChild(head);
    chip.appendChild(el("div", { class: "chip-name" }, info.name || ""));
    if (info.model) {
      chip.title = code + " — " + info.model + (eliminated.indexOf(code) !== -1 ? " （已淘汰）" : "");
    }
    grid.appendChild(chip);
  });
  const sub = $("roster-sub");
  if (sub) {
    sub.textContent = "Roster · " + order.length + " · alive " + survivors.length;
  }
}

/* ---------------- 选手实际模型编辑（模型迭代；三字码/总称/历史记录不变） ---------------- */

function enterModelEdit() {
  const st = state.lastState;
  if (!st) { return; }
  state.editingModels = true;
  state.modelOrig = {};
  const players = st.players || {};
  const order = st.order && st.order.length ? st.order : (st.survivors || []);
  const eliminated = st.eliminated || [];
  const grid = $("roster-grid");
  grid.innerHTML = "";
  for (const code of order) {
    const info = players[code] || {};
    state.modelOrig[code] = info.model || "";
    const cls = ["chip"];
    if (code === st.current_player && st.status !== "finished") { cls.push("current"); }
    if (eliminated.indexOf(code) !== -1) { cls.push("out"); }
    const chip = el("div", { class: cls.join(" ") });
    const head = el("div", { class: "chip-head" });
    head.appendChild(el("span", { class: "chip-code" }, code));
    chip.appendChild(head);
    chip.appendChild(el("div", { class: "chip-name" }, info.name || ""));
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "chip-model-input";
    inp.value = info.model || "";
    inp.placeholder = "实际模型名";
    inp.maxLength = 120;
    inp.dataset.code = code;
    inp.setAttribute("aria-label", code + " 实际模型");
    chip.appendChild(inp);
    grid.appendChild(chip);
  }
  $("btn-edit-models").classList.add("is-hidden");
  $("btn-models-save").classList.remove("is-hidden");
  $("btn-models-cancel").classList.remove("is-hidden");
  setMsg($("roster-msg"),
    "编辑模式：修改各选手的“实际模型”（悬停卡片的括号名称）。三字码与总称不变。回车保存，Esc 取消。", "");
}

function exitModelEdit() {
  state.editingModels = false;
  state.modelOrig = {};
  $("btn-edit-models").classList.remove("is-hidden");
  $("btn-models-save").classList.add("is-hidden");
  $("btn-models-cancel").classList.add("is-hidden");
  setMsg($("roster-msg"), "", "");
  if (state.lastState) { renderRoster(state.lastState); }
}

async function saveModelEdits() {
  // 收集改动：只提交发生变化的（空值视为非法，整体不保存）
  const updates = {};
  const empties = [];
  document.querySelectorAll(".chip-model-input").forEach((inp) => {
    const code = inp.dataset.code;
    const val = (inp.value || "").trim();
    if (!val) {
      empties.push(code);
      return;
    }
    if (val !== state.modelOrig[code]) {
      updates[code] = val;
    }
  });
  if (empties.length) {
    setMsg($("roster-msg"), "模型名不能为空：" + empties.join("、"), "err");
    return;
  }
  const n = Object.keys(updates).length;
  if (!n) {
    exitModelEdit();
    return;
  }
  const saveBtn = $("btn-models-save");
  saveBtn.disabled = true;
  const data = await api("/api/set-model", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: updates }),
  });
  saveBtn.disabled = false;
  if (data.ok) {
    // 先退出编辑态（恢复轮询渲染），再刷新
    state.editingModels = false;
    state.modelOrig = {};
    $("btn-edit-models").classList.remove("is-hidden");
    $("btn-models-save").classList.add("is-hidden");
    $("btn-models-cancel").classList.add("is-hidden");
    setMsg($("roster-msg"), "已更新 " + (data.count || n) + " 个选手的实际模型。", "ok");
    fullRefresh();
  } else {
    setMsg($("roster-msg"), data.error || "保存失败", "err");
  }
}

/* ---------------- 四步流程条 ---------------- */

function renderStepper(st, currentStep) {
  // 时间顺序：① 作答 ② 判定 ③ 出题 ④ 自证（2×2 布局：上排选手①③，下排框架②④）
  // 串行铁律：同一时刻最多一格黑。
  // - 空闲时：waiting 格 active（等待材料）
  // - 评测运行中：仅 runningStep 格 ongoing（呼吸黑），之前的格 done，之后的格保持中性
  const running = state.runningStep || null;
  const waiting = running ? running + 1 : (currentStep || (st.phase === "proposing" ? 3 : 1));
  const cur = st.current_player || "";
  const players = st.players || {};
  const whoTexts = [
    cur ? cur + " 正在改码" : "等待选手",
    "框架跑测试",
    cur ? cur + " 写下一棒需求" : "等待选手",
    "验题模型重实现",
  ];
  for (let n = 1; n <= 4; n++) {
    const el = $("step-" + n);
    el.querySelector(".tnode-who").textContent = whoTexts[n - 1];
    let cls = "tnode";
    if (running === n) {
      cls += " ongoing";        // 评测运行中（判定/自证），唯一黑格
    } else if (n < waiting) {
      cls += " done";           // 本轮已完成
    } else if (!running && n === waiting) {
      cls += " active";         // 等待材料（当前），仅空闲时显示
    }
    el.className = cls;
    if (st.status === "finished" && n === waiting) {
      el.className = "tnode";
    }
  }
}

/* ---------------- 需求与日志 ---------------- */

async function loadPrompt() {
  const box = $("prompt");
  const data = await api("/api/prompt");
  if (!data.ok) {
    box.textContent = "读取失败: " + (data.error || "");
    return;
  }
  if (data.content == null) {
    box.textContent = data.message || "等待首轮需求";
    return;
  }
  box.textContent = data.content;
}

async function loadLog() {
  const box = $("log");
  const data = await api("/api/log?limit=100");
  box.innerHTML = "";
  if (!data.ok) {
    box.appendChild(el("div", { class: "empty" }, "读取失败: " + (data.error || "")));
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    box.appendChild(el("div", { class: "empty" }, "暂无日志"));
    return;
  }
  for (const it of items) {
    const item = el("div", { class: "log-item r-" + (it.result || "INFO") });
    const head = el("div", { class: "log-head" });
    head.appendChild(el("span", { class: "log-ts" }, it.ts || ""));
    head.appendChild(el("span", { class: "log-action" }, "[" + (it.result || "INFO") + "] " + (it.action || "")));
    if (it.player) {
      const p = el("span", { class: "log-player" }, "选手 " + it.player + (it.round != null ? " · R" + it.round : ""));
      head.appendChild(p);
    }
    item.appendChild(head);
    if (it.detail) {
      item.appendChild(el("div", { class: "log-detail" }, it.detail));
    }
    box.appendChild(item);
  }
}

/* ---------------- 刷新 ---------------- */

async function refreshAll() {
  const data = await api("/api/state");
  if (data.ok) {
    renderState(data.state || {}, data.inbox || {}, data.step || null, data.mock);
  }
  loadLog();
}

async function fullRefresh() {
  await refreshAll();
  await Promise.all([loadTree(), loadPrompt()]);
}

/* ---------------- 操作：上传与判定 ---------------- */

async function uploadAnswer() {
  const input = $("answer-files");
  const msg = $("answer-msg");
  if (!input.files || !input.files.length) {
    setMsg(msg, "请先选择答题文件（.zip 或业务文件）", "err");
    return;
  }
  const fd = new FormData();
  for (const f of input.files) {
    fd.append("files", f, f.name);
  }
  setMsg(msg, "上传中…");
  $("btn-upload-answer").disabled = true;
  const data = await api("/api/answer", { method: "POST", body: fd });
  $("btn-upload-answer").disabled = false;
  if (data.ok) {
    setMsg(msg, data.message || "上传成功，等待验收", "ok");
  } else {
    setMsg(msg, data.error || "上传失败", "err");
  }
  refreshAll();
}

async function judgeAnswer() {
  const msg = $("answer-msg");
  setMsg(msg, state.mock ? "MOCK 判定中…" : "评测中…（应用文件 → 运行 pytest，可能需要一些时间）");
  state.runningStep = 2;  // ② 判定运行中
  renderStepper(state, 3);
  $("btn-judge-answer").disabled = true;
  const data = await api("/api/judge-answer", { method: "POST" });
  state.runningStep = null;
  if (data.ok) {
    const h = data.history, d = data.hidden;
    showVerdict(
      data.result,
      (h && d) ? "历史 " + h.passed + "/" + h.total + " · 隐藏 " + d.passed + "/" + d.total : "",
      data.message || ""
    );
    setMsg(msg, data.message || data.result, data.result === "PASS" ? "ok" : "err");
  } else {
    setMsg(msg, "未执行: " + (data.error || data.reason || "未知错误"), "err");
  }
  fullRefresh();
}

async function uploadProposal() {
  const msg = $("proposal-msg");
  const pf = $("proposal-prompt").files[0];
  const tf = $("proposal-test").files[0];
  if (!pf || !tf) {
    setMsg(msg, "请同时选择需求文档（.md/.txt）与隐藏测试（.py）", "err");
    return;
  }
  const fd = new FormData();
  fd.append("prompt", pf, pf.name);
  fd.append("test", tf, tf.name);
  setMsg(msg, "上传中…");
  $("btn-upload-proposal").disabled = true;
  const data = await api("/api/proposal", { method: "POST", body: fd });
  $("btn-upload-proposal").disabled = false;
  if (data.ok) {
    setMsg(msg, data.message || "上传成功，等待校验", "ok");
  } else {
    setMsg(msg, data.error || "上传失败", "err");
  }
  refreshAll();
}

async function judgeProposal() {
  const msg = $("proposal-msg");
  setMsg(msg, state.mock ? "MOCK 验题中…" : "验题中…（复制 arena → 调用验题模型 → 运行 pytest，可能耗时较长）");
  state.runningStep = 4;  // ④ 自证运行中（编号即时间顺序）
  renderStepper(state, 4);
  $("btn-judge-proposal").disabled = true;
  const data = await api("/api/judge-proposal", { method: "POST" });
  state.runningStep = null;
  if (data.ok) {
    showVerdict(
      data.result,
      (data.history && data.hidden)
        ? "历史 " + data.history.passed + "/" + data.history.total +
          " · 隐藏 " + data.hidden.passed + "/" + data.hidden.total
        : "",
      data.message || ""
    );
    setMsg(msg, data.message || data.result, data.result === "PASS" ? "ok" : "err");
  } else {
    setMsg(msg, "未执行: " + (data.error || data.reason || "未知错误"), "err");
  }
  fullRefresh();
}

async function restoreBackup() {
  const data = await api("/api/restore", { method: "POST" });
  if (data.ok) {
    showBanner(data.message || "已还原", "info");
    state.bannerHoldUntil = Date.now() + 15000;
  } else {
    showBanner("还原失败: " + (data.error || ""), "error");
  }
  fullRefresh();
}

async function injectRules() {
  const msg = $("rules-msg");
  const btn = $("btn-inject-rules");
  btn.disabled = true;
  const data = await api("/api/inject-rules", { method: "POST" });
  btn.disabled = false;
  setMsg(msg, data.message || data.error || "完成", data.ok ? "ok" : "err");
  fullRefresh();
}

/* ---------------- 顺序抽签（每场开始时；随机重排接力顺序并重置比赛进度） ---------------- */

async function drawLots() {
  const btn = $("btn-draw");
  btn.disabled = true;
  const data = await api("/api/draw", { method: "POST" });
  btn.disabled = false;
  if (!data.ok) {
    showBanner("抽签失败: " + (data.error || ""), "error");
    return;
  }
  const order = data.order || [];
  const players = (data.state && data.state.players) || {};
  await runDrawAnimation(order, players);
  showBanner("抽签完成：" + order.map((c, i) => (i + 1) + " " + c).join(" · "), "info");
  state.bannerHoldUntil = Date.now() + 15000;
  fullRefresh();
}

/* ---------------- 抽签动画（结果已由 /api/draw 决定，动画只是揭晓） ----------------
   节奏：前 5 位慢（悬念）-> 中段快 -> 最后 6 位慢（压轴），单次全长约 14 秒，
   随时点「跳过动画」瞬间补齐。 */
function runDrawAnimation(order, players) {
  return new Promise((resolve) => {
    const modal = $("draw-modal");
    const roll = $("draw-roll");
    const rollName = $("draw-roll-name");
    const posNum = $("draw-pos-num");
    const result = $("draw-result");
    const skipBtn = $("btn-draw-skip");
    const closeBtn = $("btn-draw-close");
    let i = 0;
    let rollTimer = null;
    let endTimer = null;
    let done = false;

    result.innerHTML = "";
    closeBtn.classList.add("is-hidden");
    skipBtn.classList.remove("is-hidden");
    modal.classList.remove("is-hidden");
    modal.setAttribute("aria-hidden", "false");

    const speedFor = (idx) => (idx < 5 ? 420 : (idx < order.length - 6 ? 240 : 420));

    function lockChip(code, idx) {
      const prev = result.querySelector(".draw-chip.latest");
      if (prev) { prev.classList.remove("latest"); }
      const chip = el("div", { class: "draw-chip latest" });
      chip.appendChild(el("span", { class: "pos" }, String(idx + 1)));
      chip.appendChild(document.createTextNode(" " + code));
      const info = players[code] || {};
      chip.title = (info.name || "") + (info.model ? "（" + info.model + "）" : "");
      result.appendChild(chip);
    }

    function finish() {
      done = true;
      clearInterval(rollTimer);
      roll.classList.remove("rolling");
      posNum.textContent = "—";
      roll.textContent = "完成";
      rollName.textContent = "1 号位 " + (order[0] || "") + " · 共 " + order.length + " 人，接力开始";
      skipBtn.classList.add("is-hidden");
      closeBtn.classList.remove("is-hidden");
      endTimer = setTimeout(close, 3000); // 3 秒后自动收起，也可手动点「完成」
    }

    function close() {
      clearInterval(rollTimer);
      clearTimeout(endTimer);
      modal.classList.add("is-hidden");
      modal.setAttribute("aria-hidden", "true");
      resolve();
    }

    function next() {
      if (i >= order.length) { return finish(); }
      const idx = i;
      const code = order[idx];
      posNum.textContent = String(idx + 1);
      rollName.textContent = "";
      roll.classList.remove("locked");
      roll.classList.add("rolling");
      const pool = order.slice(idx); // 只在"还没锁定的人"里滚动（含即将中签者）
      rollTimer = setInterval(() => {
        roll.textContent = pool[Math.floor(Math.random() * pool.length)];
      }, 60);
      setTimeout(() => {
        clearInterval(rollTimer);
        roll.classList.remove("rolling");
        roll.textContent = code;
        rollName.textContent = (players[code] || {}).name || "";
        roll.classList.add("locked");
        lockChip(code, idx);
        i += 1;
        setTimeout(next, 140);
      }, speedFor(idx));
    }

    // onclick 赋值（覆盖式），避免每次抽签叠加监听器
    skipBtn.onclick = () => {
      if (done) { return; }
      clearInterval(rollTimer);
      while (i < order.length) {
        lockChip(order[i], i);
        i += 1;
      }
      finish();
    };
    closeBtn.onclick = close;

    next();
  });
}

/* ---------------- MOCK 演练模式开关 ---------------- */

async function toggleMock() {
  const on = !state.mock;
  const btn = $("btn-mock");
  btn.disabled = true;
  const data = await api("/api/mock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: on }),
  });
  btn.disabled = false;
  if (!data.ok) {
    showBanner("切换失败: " + (data.error || ""), "error");
    return;
  }
  state.mock = on;
  showBanner(on ? "MOCK 演练模式已开启（判定随机模拟）" : "已回到真实评测模式", on ? "mock" : "info");
  state.bannerHoldUntil = Date.now() + 5000;
  fullRefresh();
}

/* ---------------- 工具 ---------------- */

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    node.className = v; // 本组件只用 class
  }
  if (text != null) {
    node.textContent = text;
  }
  return node;
}

/* ---------------- 绑定与启动 ---------------- */

window.addEventListener("DOMContentLoaded", () => {
  $("btn-restore").addEventListener("click", restoreBackup);
  $("btn-draw").addEventListener("click", drawLots);
  $("btn-inject-rules").addEventListener("click", injectRules);
  $("btn-upload-answer").addEventListener("click", uploadAnswer);
  $("btn-judge-answer").addEventListener("click", judgeAnswer);
  $("btn-upload-proposal").addEventListener("click", uploadProposal);
  $("btn-judge-proposal").addEventListener("click", judgeProposal);
  $("btn-edit-models").addEventListener("click", enterModelEdit);
  $("btn-models-save").addEventListener("click", saveModelEdits);
  $("btn-models-cancel").addEventListener("click", exitModelEdit);
  // MOCK 演练模式开关
  $("btn-mock").addEventListener("click", toggleMock);
  // 调试抽屉（录屏时保持关闭）
  $("btn-debug").addEventListener("click", () => $("debug-drawer").classList.toggle("is-hidden"));
  $("btn-debug-close").addEventListener("click", () => $("debug-drawer").classList.add("is-hidden"));
  // 判定定格可点击提前关闭
  $("verdict").addEventListener("click", hideVerdict);
  // 编辑模式键盘：回车保存，Esc 取消
  $("roster-grid").addEventListener("keydown", (e) => {
    if (!state.editingModels) { return; }
    if (e.key === "Enter") {
      e.preventDefault();
      saveModelEdits();
    } else if (e.key === "Escape") {
      exitModelEdit();
    }
  });

  fullRefresh();
  setInterval(refreshAll, 3000); // 实时赛况与日志
  setInterval(loadPrompt, 15000); // 需求文档低频刷新
});
