"use strict";
const apps = { astrbot: { title: "聊天与插件", path: "/" }, qwenpaw: { title: "记忆与任务", path: "/" }, napcat: { title: "QQ 登录", path: "/webui/" } };
const names = Object.keys(apps);
let current = names.includes(location.hash.slice(1)) ? location.hash.slice(1) : "astrbot";
let services = {};
let checking = false;
let connected = false;
const buttons = [...document.querySelectorAll("nav button")];

function show(name, focus = false) {
  if (!apps[name]) return;
  current = name;
  document.getElementById("open-native").href = "http://" + name + ".localhost:" + location.port + apps[name].path;
  history.replaceState(null, "", "#" + name);
  for (const button of buttons) {
    const selected = button.dataset.app === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    button.dataset.ready = String(services[button.dataset.app]?.ready === true);
    if (focus && selected) button.focus();
  }
  for (const key of names) {
    const panel = document.getElementById("panel-" + key);
    const frame = panel.querySelector("iframe");
    if (key === name && services[key]?.ready === true && !frame.hasAttribute("src")) {
      frame.src = "http://" + key + ".localhost:" + location.port + apps[key].path;
    }
    panel.hidden = key !== name || !frame.hasAttribute("src");
  }
  const ready = services[name]?.ready === true;
  document.getElementById("page-title").textContent = apps[name].title;
  document.getElementById("waiting").hidden = document.querySelector("#panel-" + name + " iframe").hasAttribute("src");
  document.getElementById("status").textContent = ready ? "原生页面已就绪 · 账号和模型在页面内配置" : connected ? "当前服务正在启动或暂时不可用" : "本地连接暂时不可用";
  document.getElementById("waiting-detail").textContent = connected ? "可以先切换到其他已就绪的页面。长时间未就绪时，请查看启动提示或 Docker Desktop。" : "请确认组合包已启动，或重新双击 start.cmd，然后再次检查。";
}

async function check() {
  if (checking) return;
  checking = true;
  try {
    const response = await fetch("/bundle-health", { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error("Unavailable");
    const body = await response.json();
    services = body.services || {};
    connected = true;
  } catch {
    services = {};
    connected = false;
  } finally {
    checking = false;
    show(current);
  }
}

for (const button of buttons) {
  button.addEventListener("click", () => show(button.dataset.app));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let index = names.indexOf(current);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = names.length - 1;
    else index = (index + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + names.length) % names.length;
    show(names[index], true);
  });
}
document.getElementById("refresh").addEventListener("click", () => {
  const frame = document.querySelector("#panel-" + current + " iframe");
  if (frame.hasAttribute("src")) frame.src = frame.src;
  void check();
});
document.getElementById("check-again").addEventListener("click", () => void check());
document.getElementById("help-toggle").addEventListener("click", (event) => {
  const help = document.getElementById("help");
  help.hidden = !help.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!help.hidden));
});
show(current);
void check();
setInterval(() => void check(), 5000);


// Updates are managed by the launcher. The portal never talks to Docker itself.
const updateDialog = document.getElementById("update-dialog");
const updateElement = id => document.getElementById("update-" + id);
const runningUpdatePhases = new Set(["checking", "updating", "restarting"]);
const updatePhaseMessages = {
  idle: "点击检查更新，看看是否有新的组合包版本。",
  checking: "正在检查新版本…",
  available: "发现新版本，可以查看更新内容后再决定是否更新。",
  updating: "正在备份并更新，请保持电脑和 Docker 运行。",
  restarting: "正在重启服务，页面会自动重新连接。",
  completed: "更新完成。刷新界面后即可使用新版本。",
  failed: "更新未完成。请在启动器中查看诊断和恢复提示。",
  rolled_back: "更新未通过检查，已恢复原版本。你可以继续使用。"
};
let updateState = { enabled: false, phase: "idle", active: false };
let updateToken = "";
let updateLoading = false;
let updatePosting = false;
let updateTimer;
let updateNotice = "";
let updateReconnect = false;
let updateLoaded = false;

function cleanUpdateMessage(value) {
  if (typeof value !== "string") return "";
  return value.replace(/[A-Za-z]:[\\/][^\s，。；;<>"']*/g, "[本机路径]")
    .replace(/\/(?:home|Users|root|opt|app|tmp|var|mnt|private|srv|run|data)(?:\/[^\s，。；;<>"']*)?/g, "[本机路径]")
    .slice(0, 600);
}

function updateIsActive() {
  return updateState.active === true || runningUpdatePhases.has(updateState.phase);
}

function validUpdateCandidate(candidate) {
  return candidate && typeof candidate === "object"
    && typeof candidate.id === "string" && /^[0-9a-f]{32}$/.test(candidate.id)
    && typeof candidate.version === "string" && /^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/.test(candidate.version)
    && candidate.components && typeof candidate.components === "object"
    && Object.keys(candidate.components).length === names.length
    && names.every(name => typeof candidate.components[name] === "string" && /^[A-Za-z0-9._+-]{1,100}$/.test(candidate.components[name]));
}

function renderUpdate() {
  const phase = updateState.phase || "idle";
  const active = updateIsActive();
  const candidate = validUpdateCandidate(updateState.candidate) ? updateState.candidate : null;
  const recoveryRequired = updateState.recovery_required === true;
  const canApply = updateState.enabled === true && updateState.update_capable !== false && !recoveryRequired && phase === "available" && candidate && typeof candidate.id === "string" && candidate.id.length > 0;
  const enabled = updateLoaded && updateState.enabled === true;
  updateElement("version").textContent = updateState.current_version || "—";
  updateElement("current").textContent = updateState.current_version ? "v" + String(updateState.current_version).replace(/^v/, "") : "";
  updateElement("label").textContent = active ? "更新进行中" : phase === "available" ? "有可用更新" : "检查更新";
  updateElement("toggle").dataset.active = String(active);
  updateElement("toggle").dataset.available = String(phase === "available");
  updateElement("target-row").hidden = !candidate;
  updateElement("target").textContent = candidate?.version || "—";
  let message = updatePhaseMessages[phase] || "请检查更新状态。";
  if (!updateLoaded) message = "正在连接更新服务…";
  else if (!enabled) message = "更新服务尚未启动。请使用新版启动器重新启动组合包，再回来检查更新。";
  else if (updateReconnect) message = active ? "正在重新连接更新服务，请保持电脑和 Docker 运行。" : "暂时无法连接更新服务，请确认组合包与 Docker 仍在运行。";
  else if (updateState.message && !["failed", "rolled_back"].includes(phase)) message = cleanUpdateMessage(updateState.message);
  updateElement("message").textContent = message;
  updateElement("status").setAttribute("aria-busy", String(active));
  const step = cleanUpdateMessage(updateState.step);
  updateElement("step").hidden = !active || !step;
  updateElement("step").textContent = step;
  const progress = updateElement("progress");
  progress.hidden = !active;
  if (typeof updateState.progress === "number" && Number.isFinite(updateState.progress)) progress.value = Math.max(0, Math.min(100, updateState.progress));
  else progress.removeAttribute("value");
  updateElement("candidate").hidden = !candidate;
  for (const component of names) updateElement(component).textContent = String(candidate?.components?.[component] || "—");
  updateElement("notes").textContent = typeof candidate?.notes === "string" && candidate.notes.trim() ? candidate.notes : "此版本暂无简要说明，可查看完整发布说明。";
  const releaseLink = updateElement("release");
  releaseLink.hidden = true;
  releaseLink.removeAttribute("href");
  try {
    const releaseUrl = new URL(candidate?.html_url);
    if (releaseUrl.protocol === "https:" && releaseUrl.hostname === "github.com") {
      releaseLink.href = releaseUrl.href;
      releaseLink.hidden = false;
    }
  } catch { /* An invalid release link is not useful to the user. */ }
  updateElement("warning").hidden = !canApply || active;
  const error = cleanUpdateMessage(updateNotice || (["failed", "rolled_back"].includes(phase) ? updateState.message : ""));
  updateElement("error").hidden = !error;
  updateElement("error").textContent = error;
  updateElement("check").disabled = !enabled || recoveryRequired || active || updatePosting || updateLoading || !updateToken;
  updateElement("check").textContent = phase === "checking" ? "正在检查…" : "检查更新";
  updateElement("apply").hidden = !canApply || active;
  updateElement("apply").disabled = updatePosting || updateLoading || !updateToken;
  updateElement("reload").hidden = phase !== "completed" || active;
}

function scheduleUpdatePoll() {
  clearTimeout(updateTimer);
  if (updateDialog.open || updateIsActive() || updatePosting) updateTimer = setTimeout(() => void readUpdateStatus(), 2000);
}

async function readUpdateStatus() {
  if (updateLoading || updatePosting) return;
  updateLoading = true;
  renderUpdate();
  try {
    const response = await fetch("/bundle-update", { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error("Update service unavailable");
    const body = await response.json();
    if (typeof body.enabled !== "boolean") throw new Error("Invalid update state");
    updateState = body;
    updateToken = typeof body.csrf_token === "string" ? body.csrf_token : "";
    updateLoaded = true;
    updateReconnect = false;
  } catch {
    // Keep an in-flight task visible through gateway restarts; do not retry POST.
    updateReconnect = true;
    if (!updateLoaded) {
      updateLoaded = true;
      updateState.enabled = false;
    }
  } finally {
    updateLoading = false;
    renderUpdate();
    scheduleUpdatePoll();
  }
}

async function postUpdate(action) {
  if (updatePosting || updateLoading || updateIsActive() || updateState.recovery_required === true || updateState.enabled !== true || !updateToken) return;
  const candidateId = updateState.candidate?.id;
  if (action === "apply" && (updateState.update_capable === false || updateState.phase !== "available" || typeof candidateId !== "string")) return;
  updatePosting = true;
  updateNotice = "";
  renderUpdate();
  let accepted = false;
  try {
    const response = await fetch("/bundle-update/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bundle-Update-Token": updateToken },
      body: JSON.stringify(action === "apply" ? { candidate_id: candidateId } : {}),
      signal: AbortSignal.timeout(15000)
    });
    const body = await response.json();
    if (response.status !== 202) {
      updateNotice = cleanUpdateMessage(body.message || "请求未被接受，请重新检查更新状态。");
    } else {
      accepted = true;
      updateState = { ...updateState, phase: action === "apply" ? "updating" : "checking", active: true, request_id: body.request_id };
    }
  } catch {
    updateNotice = "请求结果尚未确认，正在重新连接；不会重复提交更新。";
    updateReconnect = true;
    updateState = { ...updateState, phase: action === "apply" ? "updating" : "checking", active: true };
  } finally {
    updatePosting = false;
    if (accepted) updateNotice = "";
    renderUpdate();
    await readUpdateStatus();
  }
}

updateElement("toggle").addEventListener("click", () => {
  if (!updateDialog.open) updateDialog.showModal();
  void readUpdateStatus();
});
updateElement("close").addEventListener("click", () => updateDialog.close());
updateDialog.addEventListener("close", scheduleUpdatePoll);
updateDialog.addEventListener("click", event => {
  if (event.target !== updateDialog) return;
  const rect = updateDialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) updateDialog.close();
});
updateElement("check").addEventListener("click", () => void postUpdate("check"));
updateElement("apply").addEventListener("click", () => void postUpdate("apply"));
updateElement("reload").addEventListener("click", () => location.reload());
renderUpdate();
void readUpdateStatus();
