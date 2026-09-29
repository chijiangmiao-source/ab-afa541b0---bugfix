"use strict";

/* 阀组辨识页面逻辑。
 * 草稿标识 draft_id：任何录入修改都会换发新 id；任务结果只在
 * draft_id 与当前草稿一致、且任务未被取消/取代时才采纳，
 * 因此旧任务晚完成绝不会覆盖当前草稿或结论。
 */

const model = {
  states: ["S0", "S1"],
  commands: ["PING"],
  candidates: new Set(["S0", "S1"]),
  // trans[state][command] = { next, response }
  trans: {
    S0: { PING: { next: "S0", response: "X" } },
    S1: { PING: { next: "S0", response: "Y" } },
  },
};

let draftId = newDraftId();
let activeJob = null; // { jobId, draftId, cancelled }
let resultDraftId = null;
let pollTimer = null;

function newDraftId() {
  return "d-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

function markDirty() {
  draftId = newDraftId();
  const banner = document.getElementById("stale-banner");
  if (resultDraftId && resultDraftId !== draftId) {
    banner.hidden = false;
    banner.textContent = "草稿在上次复核后已被修改：当前结论属于旧草稿，旧任务完成不会覆盖本页面，请重新复核。";
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function asciiKey(s) {
  // 仅用于前端展示排序提示，排序真值由服务端保证。
  return s.split("").map(c => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
}

/* ---------------- 录入区 ---------------- */

function renderChipEditors() {
  const box = document.getElementById("states");
  box.innerHTML = "";
  model.states.forEach((name, idx) => box.appendChild(chip(name, v => renameState(idx, v))));
  const cbox = document.getElementById("commands");
  cbox.innerHTML = "";
  model.commands.forEach((name, idx) => cbox.appendChild(chip(name, v => renameCommand(idx, v))));
}

function chip(value, onCommit) {
  const el = document.createElement("span");
  el.className = "chip";
  const input = document.createElement("input");
  input.value = value;
  input.spellcheck = false;
  input.addEventListener("change", () => {
    const v = input.value.trim();
    if (v) onCommit(v);
    renderAll();
  });
  const rm = document.createElement("span");
  rm.className = "rm";
  rm.textContent = "×";
  rm.title = "删除";
  rm.addEventListener("click", () => { onCommit(null); renderAll(); });
  el.append(input, rm);
  return el;
}

function renameState(idx, v) {
  const old = model.states[idx];
  if (v === null) {
    if (model.states.length <= 2) { alert("至少保留 2 个状态"); return; }
    model.states.splice(idx, 1);
    model.candidates.delete(old);
    delete model.trans[old];
    for (const s of model.states) for (const c of model.commands)
      if (model.trans[s][c].next === old) model.trans[s][c].next = model.states[0];
  } else if (v !== old) {
    if (model.states.includes(v)) { alert("状态名重复"); return; }
    model.states[idx] = v;
    model.trans[v] = model.trans[old]; delete model.trans[old];
    if (model.candidates.has(old)) { model.candidates.delete(old); model.candidates.add(v); }
    for (const s of model.states) for (const c of model.commands)
      if (model.trans[s][c].next === old) model.trans[s][c].next = v;
  }
  markDirty();
}

function renameCommand(idx, v) {
  const old = model.commands[idx];
  if (v === null) {
    if (model.commands.length <= 1) { alert("至少保留 1 个命令"); return; }
    model.commands.splice(idx, 1);
    for (const s of model.states) delete model.trans[s][old];
  } else if (v !== old) {
    if (model.commands.includes(v)) { alert("命令重复"); return; }
    model.commands[idx] = v;
    for (const s of model.states) {
      model.trans[s][v] = model.trans[s][old]; delete model.trans[s][old];
    }
  }
  markDirty();
}

function renderCandidates() {
  const box = document.getElementById("candidates");
  box.innerHTML = "";
  [...model.states].sort((a, b) => asciiKey(a) < asciiKey(b) ? -1 : 1).forEach(s => {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = model.candidates.has(s);
    cb.addEventListener("change", () => {
      if (cb.checked) model.candidates.add(s); else model.candidates.delete(s);
      markDirty();
    });
    lab.append(cb, document.createTextNode(" " + s));
    box.appendChild(lab);
  });
}

function renderMatrix() {
  const table = document.getElementById("matrix");
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  const sortedCmds = [...model.commands].sort((a, b) => asciiKey(a) < asciiKey(b) ? -1 : 1);
  thead.innerHTML = "<tr><th>状态 ＼ 命令</th>" +
    sortedCmds.map(c => `<th>${esc(c)}</th>`).join("") + "</tr>";
  tbody.innerHTML = "";
  [...model.states].sort((a, b) => asciiKey(a) < asciiKey(b) ? -1 : 1).forEach(s => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="rowhead">${esc(s)}</td>`;
    sortedCmds.forEach(c => {
      const cell = model.trans[s]?.[c] || { next: s, response: "OK" };
      const td = document.createElement("td");
      const opts = model.states
        .map(t => `<option value="${esc(t)}"${t === cell.next ? " selected" : ""}>→ ${esc(t)}</option>`)
        .join("");
      td.innerHTML =
        `<select data-s="${esc(s)}" data-c="${esc(c)}" data-k="next">${opts}</select>
         <input class="resp-input" data-s="${esc(s)}" data-c="${esc(c)}" data-k="response"
                maxlength="32" value="${esc(cell.response)}" placeholder="ASCII 响应">`;
      td.querySelectorAll("[data-k]").forEach(inp => {
        inp.addEventListener("input", () => {
          if (!model.trans[s]) model.trans[s] = {};
          if (!model.trans[s][c]) model.trans[s][c] = { next: s, response: "OK" };
          if (inp.dataset.k === "next") model.trans[s][c].next = inp.value;
          else model.trans[s][c].response = inp.value;
          markDirty();
        });
      });
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function renderAll() {
  renderChipEditors();
  renderCandidates();
  renderMatrix();
}

/* ---------------- 提交 / 轮询 / 取消 ---------------- */

function buildPayload() {
  return {
    states: model.states,
    commands: model.commands,
    candidates: [...model.candidates],
    transitions: model.trans,
  };
}

function setStatus(msg, isErr = false) {
  const el = document.getElementById("job-status");
  el.textContent = msg;
  el.style.color = isErr ? "var(--danger)" : "var(--muted)";
}

function showError(msg) {
  const el = document.getElementById("form-error");
  if (msg) { el.hidden = false; el.textContent = msg; } else { el.hidden = true; }
}

async function submitCheck() {
  showError(null);
  if (model.candidates.size === 0) { showError("请至少选择一个候选初态"); return; }
  let resp;
  try {
    resp = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draftId, spec: buildPayload() }),
    });
  } catch (e) { showError("网络错误：" + e); return; }
  const data = await resp.json().catch(() => ({ error: "响应解析失败" }));
  if (!resp.ok) { showError(data.error || "提交失败"); return; }
  activeJob = { jobId: data.job_id, draftId: data.draft_id, cancelled: false };
  document.getElementById("cancel").hidden = false;
  setStatus("复核进行中…（可取消；草稿修改后旧任务结果将被忽略）");
  poll(0);
}

async function poll(delay) {
  if (!activeJob) return;
  pollTimer = setTimeout(async () => {
    if (!activeJob) return;
    let resp;
    try {
      resp = await fetch(`/api/jobs/${activeJob.jobId}?wait=2`, {
        headers: { "X-Draft-Id": activeJob.draftId },
      });
    } catch (e) { setStatus("轮询失败，重试中：" + e, true); poll(1000); return; }
    const data = await resp.json().catch(() => null);
    if (!data) { poll(1000); return; }

    // 核心防护：草稿已切换 / 任务被取消或取代 → 结果一律不采纳。
    if (resp.status === 409 || data.draft_id !== draftId ||
        (activeJob && activeJob.cancelled) || data.status === "superseded") {
      finishButtons();
      setStatus("该任务属于旧草稿或已被取代，结果已忽略。");
      activeJob = null;
      return;
    }
    if (data.status === "cancelled") {
      finishButtons();
      setStatus("任务已取消；已完成的旧结果不会覆盖当前草稿。");
      activeJob = null;
      return;
    }
    if (data.status === "error") {
      finishButtons();
      setStatus(""); showError(data.error || "复核失败");
      activeJob = null;
      return;
    }
    if (data.status === "done") {
      finishButtons();
      renderResult(data.result, data.draft_id);
      activeJob = null;
      return;
    }
    poll(0);
  }, delay);
}

function finishButtons() {
  document.getElementById("cancel").hidden = true;
}

async function cancelJob() {
  if (!activeJob) return;
  activeJob.cancelled = true;
  try {
    await fetch(`/api/jobs/${activeJob.jobId}/cancel`, { method: "POST" });
  } catch (_) { /* 本地标记已足够阻止结果覆盖 */ }
  finishButtons();
  setStatus("正在取消…已忽略该任务的任何迟到结果。");
}

/* ---------------- 结果渲染 ---------------- */

function beliefPairsText(node) {
  return "{" + node.pairs.map(([i, cur]) =>
    `${esc(i)}→${esc(cur)}`).join(", ") + "}";
}

function renderNode(node) {
  if (node.type === "resolved") {
    return `<span class="node-resolved">初态 = ${esc(node.initial)}</span>
      <span class="belief-desc">信念 ${beliefPairsText(node)}（#${node.belief}）</span>`;
  }
  if (node.type === "ambiguous_ref") {
    return `<span class="node-ref">回到不可辨信念 #${node.belief} ${beliefPairsText(node)}</span>`;
  }
  if (node.type === "command") {
    const head =
      `<span class="node-cmd">发送命令 ${esc(node.command)}</span>
       <span class="belief-desc">信念 ${beliefPairsText(node)}（#${node.belief}，
       本节点最坏还需 ${node.depth} 步）</span>`;
    const kids = node.branches.map(b =>
      `<li><span class="branch-label">回执</span><span class="node-resp">${esc(b.response)}</span>
         <span class="belief-desc">→ 信念 #${b.belief} ${beliefPairsText(b)}</span>
         <ul><li>${renderNode(b.node)}</li></ul></li>`).join("");
    return head + `<ul>${kids}</ul>`;
  }
  // ambiguous：可达且无法继续缩小的信念；列出各命令及其响应分支。
  const reason = node.reason === "merged"
    ? "两个候选已走到同一当前状态，此后任何命令都得到相同回执（重新混淆）"
    : "响应只能暂时区分，信念在无法归零的集合间循环（不可辨）";
  const head =
    `<span class="node-amb">✗ 不可辨信念 #${node.belief}</span>
     <span class="belief-desc">${beliefPairsText(node)} —— ${reason}</span>`;
  const cmds = node.branches.map(bc => {
    const rs = bc.responses.map(r =>
      `<li><span class="branch-label">回执</span><span class="node-resp">${esc(r.response)}</span>
         <span class="belief-desc">→ 信念 #${r.belief} ${beliefPairsText(r)}</span>
         <ul><li>${renderNode(r.node)}</li></ul></li>`).join("");
    return `<li><span class="node-cmd">命令 ${esc(bc.command)}</span><ul>${rs}</ul></li>`;
  }).join("");
  return head + `<div class="belief-desc">各命令分支：</div><ul>${cmds}</ul>`;
}

function renderResult(result, did) {
  resultDraftId = did;
  document.getElementById("stale-banner").hidden = true;
  document.getElementById("result-card").hidden = false;
  const ok = result.status === "distinguishable";
  const summary = document.getElementById("result-summary");
  summary.innerHTML =
    `<span class="pill ${ok ? "ok" : "no"}">${ok ? "可唯一辨识" : "不可完全辨识"}</span>` +
    (ok
      ? `最短最坏步数 = <b>${result.worst_case_depth}</b>；
         策略在所有最短方案中按命令 ASCII 序、响应分支序稳定裁决。`
      : `候选初态无法借助现有命令全部区分；树中给出了可达且无法继续缩小的信念状态及各命令分支。`) +
    `<div class="belief-desc" style="margin-top:6px">
       候选初态 ${result.candidates.map(esc).join(", ")} ｜ 命令
       ${result.commands.map(esc).join(", ")}</div>`;

  document.getElementById("tree").innerHTML =
    `<div class="tree"><ul><li>${renderNode(result.tree)}</li></ul></div>`;

  const bl = document.getElementById("belief-list");
  bl.innerHTML = result.beliefs.map(b => {
    const cls = "belief-row" + (b.terminal ? " term" : b.winning ? "" : " lose");
    const state = b.terminal ? "已收敛" : b.winning
      ? `可辨识（最坏 ${b.depth} 步）`
      : (b.merged ? "不可辨·重新混淆" : "不可辨·循环");
    return `<div class="${cls}"><span class="bid">#${b.id}</span>
      <span>${beliefPairsText(b)}</span><span>${state}</span></div>`;
  }).join("");
  document.getElementById("belief-explorer").open = !ok;
  setStatus("复核完成。");
}

/* ---------------- 初始化 ---------------- */

document.getElementById("add-state").addEventListener("click", () => {
  if (model.states.length >= 10) { alert("状态至多 10 个"); return; }
  let name = "S" + model.states.length;
  while (model.states.includes(name)) name += "_";
  model.states.push(name);
  model.trans[name] = {};
  model.commands.forEach(c => (model.trans[name][c] = { next: name, response: "OK" }));
  markDirty(); renderAll();
});
document.getElementById("add-command").addEventListener("click", () => {
  if (model.commands.length >= 6) { alert("命令至多 6 个"); return; }
  let name = "CMD" + model.commands.length;
  while (model.commands.includes(name)) name += "_";
  model.commands.push(name);
  model.states.forEach(s => (model.trans[s][name] = { next: s, response: "OK" }));
  markDirty(); renderAll();
});
document.getElementById("submit").addEventListener("click", submitCheck);
document.getElementById("cancel").addEventListener("click", cancelJob);
document.getElementById("fill-self").addEventListener("click", () => {
  model.states.forEach(s => model.commands.forEach(c => {
    if (!model.trans[s]) model.trans[s] = {};
    if (!model.trans[s][c]) model.trans[s][c] = { next: s, response: "OK" };
  }));
  markDirty(); renderMatrix();
});

renderAll();
