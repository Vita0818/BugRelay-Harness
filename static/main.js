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
  selectedFile: null,
  pendingAnswer: null,
  pendingProposal: null,
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

/* ---------------- 赛况渲染 ---------------- */

function renderState(st, inbox) {
  state.arenaReady = !!st.arena_ready;
  state.phase = st.phase;
  const inboxAnswer = inbox && inbox.answer;
  const inboxProposal = inbox && inbox.proposal;

  $("st-round").textContent = st.round;
  $("st-player").textContent = st.current_player || "–";
  const phaseMap = { answering: "答题中", proposing: "出题中" };
  const phaseEl = $("st-phase");
  phaseEl.textContent = phaseMap[st.phase] || st.phase || "–";
  phaseEl.className = st.phase === "proposing" ? "warn" : "";

  $("st-survivors").textContent = (st.survivors || []).join(" ") || "（无）";
  $("st-survivors").className = (st.survivors || []).length ? "ok" : "bad";
  $("st-eliminated").textContent = (st.eliminated || []).join(" ") || "（无）";
  $("st-eliminated").className = (st.eliminated || []).length ? "bad" : "";

  const arenaEl = $("st-arena");
  arenaEl.textContent = st.arena_ready ? "就绪" : "未就绪";
  arenaEl.className = st.arena_ready ? "ok" : "bad";

  const lastEl = $("st-last");
  lastEl.textContent = st.last_result || "–";
  lastEl.className = st.last_result === "PASS" ? "ok" : (st.last_result === "FAIL" ? "bad" : "");

  if (st.status === "finished") {
    showBanner("比赛已结束：无存活选手可继续接力。", "warn");
  } else if (!st.arena_ready) {
    showBanner("arena_repo 未就绪：请确认 config.json 的 arena_repo_path 指向已存在的独立 git 仓库（含 src/ 与 tests/）。在此之前仅能浏览页面，无法评测。", "warn");
  } else {
    hideBanner();
  }

  // 底部两行测试结果（只显示总结果与计数）
  const sum = st.last_test_summary;
  renderTestRow("t-history", sum && sum.history);
  renderTestRow("t-hidden", sum && sum.hidden);

  // 按钮可用性（已导入材料 或 inbox/ 中检测到材料均可直接判定）
  $("btn-judge-answer").disabled = !(st.arena_ready && st.phase === "answering" && (st.pending_answer || inboxAnswer));
  $("btn-judge-proposal").disabled = !(st.arena_ready && st.phase === "proposing" && (st.pending_proposal || inboxProposal));
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
      row.appendChild(document.createTextNode("📁 " + n.name));
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
      row.appendChild(document.createTextNode("📄 " + n.name));
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
    renderState(data.state || {}, data.inbox || {});
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
  if (!confirm("确认验收当前选手的答题？将应用已上传的业务文件并运行历史+隐藏测试。")) {
    return;
  }
  setMsg(msg, "评测中…（应用文件 → 运行 pytest，可能需要一些时间）");
  $("btn-judge-answer").disabled = true;
  const data = await api("/api/judge-answer", { method: "POST" });
  if (data.ok) {
    const line = (data.result === "PASS") ? "✔ " : "✘ ";
    const h = data.history, d = data.hidden;
    let text = line + (data.message || data.result);
    if (h && d) {
      text += "\n历史测试 " + h.passed + "/" + h.total + " · 隐藏测试 " + d.passed + "/" + d.total;
    }
    setMsg(msg, text, data.result === "PASS" ? "ok" : "err");
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
  if (!confirm("确认校验出题并交棒？将调用验题模型自证（单次调用，不重试），全绿才交棒。")) {
    return;
  }
  setMsg(msg, "验题中…（复制 arena → 调用验题模型 → 运行 pytest，可能耗时较长）");
  $("btn-judge-proposal").disabled = true;
  const data = await api("/api/judge-proposal", { method: "POST" });
  if (data.ok) {
    const line = (data.result === "PASS") ? "✔ " : "✘ ";
    let text = line + (data.message || data.result);
    if (data.history && data.hidden) {
      text += "\n历史测试 " + data.history.passed + "/" + data.history.total +
              " · 隐藏测试 " + data.hidden.passed + "/" + data.hidden.total;
    }
    setMsg(msg, text, data.result === "PASS" ? "ok" : "err");
  } else {
    setMsg(msg, "未执行: " + (data.error || data.reason || "未知错误"), "err");
  }
  fullRefresh();
}

async function restoreBackup() {
  if (!confirm("确认把 arena_repo 还原到最近一次备份？当前未备份的改动将丢失。")) {
    return;
  }
  const data = await api("/api/restore", { method: "POST" });
  if (data.ok) {
    showBanner(data.message || "已还原", "info");
  } else {
    showBanner("还原失败: " + (data.error || ""), "error");
  }
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
  $("btn-refresh").addEventListener("click", fullRefresh);
  $("btn-restore").addEventListener("click", restoreBackup);
  $("btn-upload-answer").addEventListener("click", uploadAnswer);
  $("btn-judge-answer").addEventListener("click", judgeAnswer);
  $("btn-upload-proposal").addEventListener("click", uploadProposal);
  $("btn-judge-proposal").addEventListener("click", judgeProposal);

  fullRefresh();
  setInterval(refreshAll, 3000); // 实时赛况与日志
  setInterval(loadPrompt, 15000); // 需求文档低频刷新
});
