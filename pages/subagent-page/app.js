let refreshTimerManage = null;
let refreshTimerTasks = null;
let refreshTimerSessions = null;
let editingName = null;
let activeTab = "manage";

// === Utilities ===

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function showError(message) {
  const old = document.getElementById("page-error");
  if (old) old.remove();
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div id="page-error" class="error">${escapeHtml(message)}</div>`
  );
}

function showNotice(message) {
  const old = document.getElementById("page-notice");
  if (old) old.remove();
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div id="page-notice" class="notice">${escapeHtml(message)}</div>`
  );
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtDuration(start, end) {
  if (!start || !end) return "-";
  const s = Math.round(end - start);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60) + "s";
  return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m";
}

function getBridge() {
  const bridge = window.AstrBotPluginPage;
  if (!bridge) throw new Error("AstrBotPluginPage bridge 未加载");
  return bridge;
}

// 修 #5 bug 关键：window.confirm() 在 iframe / sandboxed / CSP 环境下可能：
//   1. 抛 TypeError（confirm **不**是 function）
//   2. 抛 SecurityError（sandbox 阻**止** modal dialog）
//   3. 返 undefined（some 浏览器静默 disable）
// **任**何**这**些**都**会**让** deleteSession / deleteSubagent 在
// `if (!confirm(...))` 处**静**默**早**期** return（**或**崩）→ 主**人**点** delete **完**全**没**反**应**。
//
// confirmDialog() 防御性 helper：
//   1. 先试 window.confirm（保**留**主**人**熟**悉**的 native 弹窗）
//   2. 如**果** confirm 不存**在** / 抛错 / 返 undefined → 用 inline <div> 模态 fallback
// inline 模态是 plugin 自己控**制**的 markup + event listener，**不**依赖 native API，
// **在** iframe / sandboxed / CSP 环境**下** **永**远**可**用**。
function confirmDialog(message) {
  return new Promise((resolve) => {
    // 直接走 inline overlay——不在 sandboxed iframe 里试 window.confirm：
    // AstrBot WebUI 把插件页跑在 sandboxed iframe 下，'allow-modals' 不开，
    // window.confirm() 会被浏览器静默吞掉（只打 console warning，返 undefined），
    // 让「点删除没反应」的 bug 复现。inline overlay 不依赖 native API，永远可用。

    const doc = document;
    const overlay = doc.createElement("div");
    overlay.setAttribute("data-confirm-overlay", "");
    Object.assign(overlay.style, {
      position: "fixed",
      inset: "0",
      background: "rgba(0, 0, 0, 0.5)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      zIndex: "2147483647",  // 最大值，避开一切 stacking context
    });

    const box = doc.createElement("div");
    Object.assign(box.style, {
      background: "#fff",
      color: "#000",
      borderRadius: "8px",
      padding: "20px",
      maxWidth: "460px",
      width: "90%",
      boxShadow: "0 10px 40px rgba(0,0,0,0.3)",
      fontFamily: "system-ui, -apple-system, sans-serif",
    });

    const title = doc.createElement("h3");
    title.textContent = "确认操作";
    Object.assign(title.style, { margin: "0 0 12px 0", fontSize: "16px" });

    const msg = doc.createElement("p");
    msg.textContent = message;
    Object.assign(msg.style, {
      margin: "0 0 20px 0",
      lineHeight: "1.5",
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
    });

    const actions = doc.createElement("div");
    Object.assign(actions.style, {
      display: "flex",
      justifyContent: "flex-end",
      gap: "8px",
    });

    const cancelBtn = doc.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "取消";
    Object.assign(cancelBtn.style, {
      padding: "8px 16px",
      border: "1px solid #ccc",
      borderRadius: "4px",
      background: "#fff",
      color: "#000",
      cursor: "pointer",
      fontSize: "14px",
    });

    const okBtn = doc.createElement("button");
    okBtn.type = "button";
    okBtn.textContent = "确认";
    Object.assign(okBtn.style, {
      padding: "8px 16px",
      border: "none",
      borderRadius: "4px",
      background: "#1976d2",
      color: "#fff",
      cursor: "pointer",
      fontSize: "14px",
    });

    const cleanup = () => {
      try { doc.body.removeChild(overlay); } catch (_) {}
      okBtn.onclick = null;
      cancelBtn.onclick = null;
    };
    okBtn.onclick = () => { cleanup(); resolve(true); };
    cancelBtn.onclick = () => { cleanup(); resolve(false); };

    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    box.appendChild(title);
    box.appendChild(msg);
    box.appendChild(actions);
    overlay.appendChild(box);
    doc.body.appendChild(overlay);

    // ESC 键取消
    const onKey = (e) => {
      if (e.key === "Escape") {
        cleanup();
        doc.removeEventListener("keydown", onKey);
        resolve(false);
      }
    };
    doc.addEventListener("keydown", onKey);
  });
}

// === Tab switching ===

function switchTab(name) {
  if (name !== "manage" && name !== "tasks" && name !== "sessions") return;
  activeTab = name;
  document.querySelectorAll(".tab").forEach((btn) => {
    const isActive = btn.dataset.tab === name;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.tab !== name);
  });
  // 切换时立刻拉一次当前 tab 的数据，并同步 hash 方便刷新保留
  if (name === "manage") refreshManage();
  else if (name === "tasks") refreshTasks();
  else if (name === "sessions") refreshSessions();
  if (location.hash.slice(1) !== name) {
    history.replaceState(null, "", "#" + name);
  }
}

// === Tab: 实例管理 ===

function openModalForCreate() {
  editingName = null;
  document.getElementById("modal-title").textContent = "新增子 AstrBot";
  document.getElementById("field-name").disabled = false;
  document.getElementById("field-name").value = "";
  document.getElementById("field-base-url").value = "";
  document.getElementById("field-token").value = "";
  document.getElementById("field-token").placeholder = "abk_xxx（新增必填）";
  document.getElementById("field-description").value = "";
  document.getElementById("field-username").value = "";
  document.getElementById("field-enabled").checked = true;
  document.getElementById("field-verify-ssl").checked = true;
  document.getElementById("modal").classList.remove("hidden");
}

function openModalForEdit(sa) {
  editingName = sa.name;
  document.getElementById("modal-title").textContent = `编辑：${sa.name}`;
  document.getElementById("field-name").disabled = true;
  document.getElementById("field-name").value = sa.name || "";
  document.getElementById("field-base-url").value = sa.base_url || "";
  document.getElementById("field-token").value = "";
  document.getElementById("field-token").placeholder = "留空 = 保留原 token；填新值 = 替换";
  document.getElementById("field-description").value = sa.description || "";
  document.getElementById("field-username").value = sa.username || "";
  document.getElementById("field-enabled").checked = !!sa.enabled;
  document.getElementById("field-verify-ssl").checked = sa.verify_ssl !== false;
  document.getElementById("modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  editingName = null;
}

async function saveSubagent(event) {
  event.preventDefault();
  const name = document.getElementById("field-name").value.trim();
  const base_url = document.getElementById("field-base-url").value.trim();
  const tokenInput = document.getElementById("field-token").value.trim();
  const description = document.getElementById("field-description").value.trim();
  const username = document.getElementById("field-username").value.trim();
  const enabled = document.getElementById("field-enabled").checked;
  const verify_ssl = document.getElementById("field-verify-ssl").checked;

  if (!name || !base_url) {
    showError("名称 / Base URL 为必填");
    return;
  }

  const btn = document.getElementById("btn-save");
  btn.disabled = true;
  btn.textContent = "保存中...";
  try {
    const bridge = getBridge();
    const payload = { name, base_url, description, username, enabled, verify_ssl };
    if (tokenInput) {
      payload.token = tokenInput;
    }
    const data = await bridge.apiPost("subagents/upsert", payload);
    if (!data || !data.ok) throw new Error(data?.error || "保存失败");
    showNotice(`已${editingName ? "更新" : "新增"}：${data.name}`);
    closeModal();
    await refreshManage();
  } catch (e) {
    showError(e?.message || e || "保存失败");
  } finally {
    btn.disabled = false;
    btn.textContent = "保存";
  }
}

async function deleteSubagent(name, btn) {
  if (!name) return;
  if (!await confirmDialog(`确定删除子 AstrBot "${name}"？\n此操作不会影响已提交的后台任务。`)) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "删除中...";
  }
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("subagents/delete", { name });
    if (!data || !data.ok) throw new Error(data?.error || "删除失败");
    showNotice(`已删除：${name}`);
    await refreshManage();
  } catch (e) {
    showError(e?.message || e || "删除失败");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "删除";
    }
  }
}

async function toggleSubagent(name, btn) {
  if (!name) return;
  const isEnabled = btn.dataset.enabled === "1";
  const newEnabled = !isEnabled;
  btn.disabled = true;
  btn.textContent = "切换中...";
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("subagents/toggle", {
      name, enabled: newEnabled,
    });
    if (!data || !data.ok) throw new Error(data?.error || "切换失败");
    showNotice(`已${newEnabled ? "启用" : "禁用"}：${name}`);
    await refreshManage();
  } catch (e) {
    showError(e?.message || e || "切换失败");
    btn.disabled = false;
    btn.textContent = isEnabled ? "禁用" : "启用";
  }
}

async function pingSubagent(name, btn) {
  if (!name) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "检查中...";
  }
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("subagents/ping", { name });
    if (!data || !data.ok) throw new Error(data?.error || "ping 失败");
    if (data.ping_ok) {
      showNotice(`✅ ${name} 健康检查通过`);
    } else {
      showError(`❌ ${name} 健康检查失败：${data.ping_msg || "未知错误"}`);
    }
    await refreshManage();
  } catch (e) {
    showError(e?.message || e || "ping 失败");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Ping";
    }
  }
}

function renderManageRow(sa) {
  const name = escapeHtml(sa.name || "");
  const baseUrl = escapeHtml(sa.base_url || "");
  const token = escapeHtml(sa.token || "");
  const description = escapeHtml(sa.description || "");
  const username = escapeHtml(sa.username || "");
  const usernameCell = username
    ? `<td class="username" title="${username}">${username}</td>`
    : `<td class="username" title="fallback 到主控 sender_id"><span class="muted">fallback</span></td>`;
  const isEnabled = !!sa.enabled;
  const enabledBadge = isEnabled
    ? `<span class="badge enabled">已启用</span>`
    : `<span class="badge disabled">已禁用</span>`;
  const verifyVal = sa.verify_ssl === undefined ? true : !!sa.verify_ssl;
  const sslBadge = verifyVal
    ? `<span class="badge enabled" title="校验证书链">🔒 SSL</span>`
    : `<span class="badge warn" title="跳过证书校验——Bearer Token 明文传输，仅限自签名 / 内网">⚠ SSL 跳过</span>`;

  let pingCell = `<span class="muted">-</span>`;
  if (typeof sa.ping_ok === "boolean") {
    if (sa.ping_ok) {
      pingCell = `<span class="ping-ok">✅ 通</span>`;
    } else {
      pingCell = `<span class="ping-fail" title="${escapeHtml(sa.ping_msg || "")}">❌ ${escapeHtml(sa.ping_msg || "失败")}</span>`;
    }
  }

  const toggleLabel = isEnabled ? "禁用" : "启用";
  const actions = `
    <td class="actions">
      <button class="small" data-action="ping" data-name="${name}">Ping</button>
      <button class="small" data-action="edit" data-name="${name}">编辑</button>
      <button class="small" data-action="toggle" data-name="${name}" data-enabled="${isEnabled ? "1" : "0"}">${toggleLabel}</button>
      <button class="small danger" data-action="delete" data-name="${name}">删除</button>
    </td>
  `;

  return `<tr>
    <td><b>${name}</b></td>
    <td class="url">${baseUrl}</td>
    <td class="token">${token}</td>
    <td class="description" title="${description}">${description}</td>
    ${usernameCell}
    <td>${enabledBadge}</td>
    <td>${sslBadge}</td>
    <td>${pingCell}</td>
    <td>${fmtTime(sa.updated_at)}</td>
    ${actions}
  </tr>`;
}

function bindManageActionButtons() {
  // 改为事件委托：见 bindActionDelegation()。
  // 保留空函数名（无害 stub）以避免未来 grep 误判；实际逻辑在 delegation 里。
}

async function refreshManage() {
  let data;
  try {
    const bridge = getBridge();
    data = await bridge.apiGet("subagents");
  } catch (e) {
    showError(e?.message || e || "Plugin Page API 调用失败");
    return;
  }
  if (!data || !data.ok) {
    showError(data?.error || "Plugin Page API 返回异常");
    return;
  }
  const oldError = document.getElementById("page-error");
  if (oldError) oldError.remove();

  const items = data.subagents || [];
  const enabled = items.filter((s) => s.enabled).length;
  setText("cnt-total", items.length);
  setText("cnt-enabled", enabled);
  setText("cnt-disabled", items.length - enabled);

  const tbody = document.querySelector("#tbl-subagents tbody");
  if (tbody) {
    tbody.innerHTML = items.length === 0
      ? `<tr><td colspan="10" class="empty">尚未注册任何子 AstrBot。点击右上角「+ 新增」开始。</td></tr>`
      : items.map(renderManageRow).join("");
  }
}

// === Tab: 任务列表 ===

function badge(status) {
  const valid = ["running", "done", "failed", "cancelled", "no_recipient"];
  const cls = valid.includes(status) ? status : "failed";
  return `<span class="badge ${cls}">${escapeHtml(status || "unknown")}</span>`;
}

function tasksActionsCell(t) {
  const taskId = escapeHtml(t.task_id || "");
  const isRunning = t.status === "running";
  const cancelBtn = isRunning
    ? `<button class="warn" data-action="cancel" data-task-id="${taskId}">取消</button> `
    : "";
  const deleteBtn = `<button class="danger" data-action="delete" data-task-id="${taskId}">删除</button>`;
  return `<td>${cancelBtn}${deleteBtn}</td>`;
}

function renderTaskRow(t, includeStatus) {
  const subagent = escapeHtml(t.subagent || "");
  const taskText = escapeHtml(t.task_text || "");
  const mode = escapeHtml(t.mode || "");
  const taskId = escapeHtml(t.task_id || "");
  const idCell = `<td class="id">${taskId}</td>`;
  if (includeStatus) {
    return `<tr>
      <td><b>${subagent}</b></td>
      <td class="task" title="${taskText}">${taskText}</td>
      <td>${badge(t.status)}</td>
      <td>${mode}</td>
      <td>${fmtTime(t.created_at)}</td>
      <td>${fmtTime(t.finished_at)}</td>
      <td>${fmtDuration(t.created_at, t.finished_at)}</td>
      ${idCell}
      ${tasksActionsCell(t)}
    </tr>`;
  } else {
    return `<tr>
      <td><b>${subagent}</b></td>
      <td class="task" title="${taskText}">${taskText}</td>
      <td>${mode}</td>
      <td>${fmtTime(t.created_at)}</td>
      ${idCell}
      ${tasksActionsCell(t)}
    </tr>`;
  }
}

async function cancelTask(taskId, btn) {
  if (!taskId) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "取消中...";
  }
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("cancel", { task_id: taskId });
    if (!data || !data.ok) throw new Error(data?.error || "取消失败");
    showNotice(`已取消任务 ${taskId}`);
    await refreshTasks();
  } catch (e) {
    showError(e?.message || e || "取消失败");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "取消";
    }
  }
}

async function deleteTask(taskId, btn) {
  if (!taskId) return;
  if (!await confirmDialog(`彻底删除任务 ${taskId}？\n（仅清 plugin 端 audit 记录——若还在跑会同时取消）\n此操作不可撤销。`)) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "删除中...";
  }
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("delete", { task_id: taskId });
    if (!data || !data.ok) throw new Error(data?.error || "删除失败");
    showNotice(`已删除任务 ${taskId}`);
    await refreshTasks();
  } catch (e) {
    showError(e?.message || e || "删除失败");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "删除";
    }
  }
}

function bindTasksActionButtons() {
  // 改为事件委托：见 bindActionDelegation()。
  // 保留空函数名（无害 stub）以避免未来 grep 误判；实际逻辑在 delegation 里。
}

async function refreshTasks() {
  let data;
  try {
    const bridge = getBridge();
    data = await bridge.apiGet("tasks");
  } catch (e) {
    showError(e?.message || e || "Plugin Page API 调用失败");
    return;
  }
  if (!data || !data.ok) {
    showError(data?.error || "Plugin Page API 返回异常");
    return;
  }
  const oldError = document.getElementById("page-error");
  if (oldError) oldError.remove();
  const tasks = data.tasks || [];
  const running = tasks.filter((t) => t.status === "running");
  const history = tasks.filter((t) => t.status !== "running");

  setText("cnt-running", data.running_count ?? running.length);
  setText("cnt-done", history.filter((t) => t.status === "done").length);
  setText("cnt-failed", history.filter((t) => t.status === "failed").length);
  setText("cnt-cancelled", history.filter((t) => t.status === "cancelled").length);
  setText("cnt-no-recipient", history.filter((t) => t.status === "no_recipient").length);
  setText("cnt-total-tasks", data.total_count ?? tasks.length);

  const tbR = document.querySelector("#tbl-running tbody");
  if (tbR) {
    tbR.innerHTML = running.length === 0
      ? `<tr><td colspan="6" class="empty">无运行中任务</td></tr>`
      : running.map((t) => renderTaskRow(t, false)).join("");
  }

  const tbH = document.querySelector("#tbl-history tbody");
  if (tbH) {
    tbH.innerHTML = history.length === 0
      ? `<tr><td colspan="9" class="empty">无历史任务</td></tr>`
      : history.map((t) => renderTaskRow(t, true)).join("");
  }
}

// === Tab: 项目会话（plugin 不存 history 详情，只展示 astrbot session_id 映射）===

function renderSessionRow(s) {
  const subagent = escapeHtml(s.subagent || "");
  const project = escapeHtml(s.project || "");
  // 子 AstrBot 的 session_id（plugin 调子 AstrBot 时传这个续上下文）
  const astrbotSid = s.astrbot_session_id || "";
  const astrbotSidHtml = astrbotSid
    ? `<code title="${escapeHtml(astrbotSid)}">${escapeHtml(astrbotSid.length > 16 ? astrbotSid.slice(0, 16) + "…" : astrbotSid)}</code>`
    : `<span class="muted">（首次调用时自动建）</span>`;
  const userId = escapeHtml(s.user_id || "");
  // plugin 内部 session_id (PK, ps-...)——主要用于 audit / 日志
  const pluginSid = escapeHtml(s.session_id || "");
  const pluginSidShort = pluginSid.length > 12 ? pluginSid.slice(0, 12) + "…" : pluginSid;
  const deleteAction = `<button class="danger" data-action="delete-session" data-subagent="${subagent}" data-project="${project}">删除</button>`;
  return `<tr>
    <td class="subagent-name"><b>${subagent}</b></td>
    <td class="project-name">${project}</td>
    <td class="session-id">${astrbotSidHtml}</td>
    <td class="muted">${userId}</td>
    <td>${fmtTime(s.created_at)}</td>
    <td>${fmtTime(s.updated_at)}</td>
    <td class="muted" title="${escapeHtml(pluginSid)}">${escapeHtml(pluginSidShort)}</td>
    <td class="actions">${deleteAction}</td>
  </tr>`;
}

function bindSessionsActionButtons() {
  // 改为事件委托：见 bindActionDelegation()。
  // 保留空函数名（无害 stub）以避免未来 grep 误判；实际逻辑在 delegation 里。
}

// === 事件委托 — 修 #3 bug：8s timer 重渲染 tbody 后新 button 没绑 onclick ===
// 主人 2026-06-20 02:23 反馈：项目会话 tab「删除」button **没反应**。
// 根因：旧 bindXxxActionButtons() 只在 refresh 末尾调，timer 重渲染时新 button
// 没绑 onclick（race condition）。
// 修法：在每个 panel 上 addEventListener **一次性**（init 时绑，不在每次 refresh 重绑），
// handler 里 e.target.closest('button[data-action]') 找 button，switch on dataset.action
// 分派到对应函数。
// 收益：DOM 重新渲染**也**响应（不依赖 bind 时机）；8s timer / tab 切换 / 手动刷新
// / 任意时机都稳定；未来加新 button（data-action="xxx"）只需在 switch 里加 case。
function bindActionDelegation() {
  // 防御性 try/catch — 任何子 handler 抛错**不**影响后续 panel 注册
  // （**不**写 console.error 到生产代码——silently swallow；showError 由各 handler 自己负责）
  // === panel-manage: ping / edit / toggle / delete ===
  const managePanel = document.getElementById("panel-manage");
  if (managePanel) {
    managePanel.addEventListener("click", (e) => {
      try {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const action = btn.dataset.action;
        const name = btn.dataset.name;
        switch (action) {
          case "ping":
            pingSubagent(name, btn);
            break;
          case "edit":
            // 编辑按钮需要先 GET 实例详情——异步，IIFE 包一下避免 handler 自身 async
            (async () => {
              try {
                const bridge = getBridge();
                const data = await bridge.apiGet("subagents");
                const sa = (data?.subagents || []).find((x) => x.name === name);
                if (sa) openModalForEdit(sa);
              } catch (err) {
                showError(err?.message || err || "读取实例失败");
              }
            })();
            break;
          case "toggle":
            toggleSubagent(name, btn);
            break;
          case "delete":
            deleteSubagent(name, btn);
            break;
        }
      } catch (_) { /* silent */ }
    });
  }

  // === panel-tasks: cancel / delete ===
  const tasksPanel = document.getElementById("panel-tasks");
  if (tasksPanel) {
    tasksPanel.addEventListener("click", (e) => {
      try {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const action = btn.dataset.action;
        const taskId = btn.dataset.taskId;
        switch (action) {
          case "cancel":
            cancelTask(taskId, btn);
            break;
          case "delete":
            deleteTask(taskId, btn);
            break;
        }
      } catch (_) { /* silent */ }
    });
  }

  // === panel-sessions: delete-session ===
  const sessionsPanel = document.getElementById("panel-sessions");
  if (sessionsPanel) {
    sessionsPanel.addEventListener("click", (e) => {
      try {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const action = btn.dataset.action;
        const subagent = btn.dataset.subagent;
        const project = btn.dataset.project;
        switch (action) {
          case "delete-session":
            deleteSession(subagent, project, btn);
            break;
        }
      } catch (_) { /* silent */ }
    });
  }
}

async function refreshSessions() {
  let data;
  try {
    const bridge = getBridge();
    data = await bridge.apiGet("project_sessions");
  } catch (e) {
    showError(e?.message || e || "Plugin Page API 调用失败");
    return;
  }
  if (!data || !data.ok) {
    showError(data?.error || "Plugin Page API 返回异常");
    return;
  }
  const oldError = document.getElementById("page-error");
  if (oldError) oldError.remove();
  const items = data.sessions || [];
  setText("cnt-sessions", items.length);

  const tbody = document.querySelector("#tbl-sessions tbody");
  if (tbody) {
    tbody.innerHTML = items.length === 0
      ? `<tr><td colspan="8" class="empty">尚无活跃项目 session。<br>用 /subagent_chat &lt;name&gt; &lt;project&gt; &lt;消息&gt; 创建第一个。<br><span class="muted" style="font-size:12px;">首次调用 plugin 不传 session_id → 子 AstrBot 自动建 UUID → plugin 捕获并写入本表。</span></td></tr>`
      : items.map(renderSessionRow).join("");
  }
}

async function deleteSession(subagent, project, btn) {
  if (!subagent || !project) return;
  if (!await confirmDialog(`彻底删除项目 session "${subagent} :: ${project}" 的 plugin 映射？\n效果同「删 mapping」——下次同 key 调一次，子 AstrBot 自动建新 UUID。\n（旧 history 仍在子 AstrBot 那边，要彻底断绝去子 AstrBot WebUI。）`)) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "删除中...";
  }
  try {
    const bridge = getBridge();
    const data = await bridge.apiPost("project_sessions/clear", { subagent, project });
    if (!data || !data.ok) throw new Error(data?.error || "删除失败");
    showNotice(`已删除：${subagent} :: ${project}`);
    await refreshSessions();
  } catch (e) {
    showError(e?.message || e || "删除失败");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "删除";
    }
  }
}

// === Timers ===
// 实例管理 tab 10s 刷新，任务列表 tab 5s 刷新（任务状态变更更频繁）
// 项目会话 tab 8s 刷新（适中——history 不像 task 那样高频变更）

function startTimers() {
  if (!refreshTimerManage) refreshTimerManage = setInterval(refreshManage, 10000);
  if (!refreshTimerTasks) refreshTimerTasks = setInterval(refreshTasks, 5000);
  if (!refreshTimerSessions) refreshTimerSessions = setInterval(refreshSessions, 8000);
}

function stopTimers() {
  if (refreshTimerManage) { clearInterval(refreshTimerManage); refreshTimerManage = null; }
  if (refreshTimerTasks) { clearInterval(refreshTimerTasks); refreshTimerTasks = null; }
  if (refreshTimerSessions) { clearInterval(refreshTimerSessions); refreshTimerSessions = null; }
}

// === Init ===

(async () => {
  try {
    // 修 #4 bug：事件委托绑在 await bridge.ready() **之**前**——
    // 即便 bridge.ready() hang / 抛错，3 个 panel 上的 click delegation **仍**绑**了**，
    // 主人**点** delete **不**会**没**反**应**。同时每个 addEventListener 单**独**
    // try/catch（**不**让**一**个** getElementById 返 null 毁**后**续）。
    bindActionDelegation();
    // **局**部 helper：防御性绑 click——任何异常静默吞掉（**不**写 console.error）
    const _bindClick = (id, fn) => {
      try {
        const el = document.getElementById(id);
        if (el && typeof fn === "function") el.addEventListener("click", fn);
      } catch (_) { /* silent */ }
    };
    const _bindSubmit = (id, fn) => {
      try {
        const el = document.getElementById(id);
        if (el && typeof fn === "function") el.addEventListener("submit", fn);
      } catch (_) { /* silent */ }
    };

    const bridge = getBridge();
    await bridge.ready();

    // Tabs
    try {
      document.querySelectorAll(".tab").forEach((btn) => {
        try { btn.addEventListener("click", () => switchTab(btn.dataset.tab)); } catch (_) {}
      });
    } catch (_) {}

    // 实例管理 buttons
    _bindClick("refresh-manage", refreshManage);
    _bindClick("add-new", openModalForCreate);
    _bindClick("modal-close", closeModal);
    _bindClick("btn-cancel", closeModal);
    _bindSubmit("form-subagent", saveSubagent);

    document.getElementById("modal")?.addEventListener("click", (e) => {
      if (e.target.id === "modal") closeModal();
    });

    // 任务列表 buttons
    _bindClick("refresh-tasks", refreshTasks);

    // 项目会话 buttons
    _bindClick("refresh-sessions", refreshSessions);

    // 初始 tab：从 URL hash 读（默认 manage）
    let initialTab = "manage";
    const hash = location.hash.slice(1);
    if (hash === "tasks" || hash === "sessions" || hash === "manage") {
      initialTab = hash;
    }
    switchTab(initialTab);

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopTimers();
      else { refreshManage(); refreshTasks(); refreshSessions(); startTimers(); }
    });
    startTimers();
  } catch (e) {
    showError(e?.message || e || "Plugin Page 初始化失败");
  }
})();