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

// Backups use a separate explicit workflow. Uploading never starts a restore.
const backupDialog = document.getElementById("backup-dialog");
const backupElement = id => document.getElementById("backup-" + id);
const backupTabs = ["export", "import", "list"];
const backupActivePhases = new Set(["exporting", "inspecting", "restoring"]);
const backupMessages = {
  idle: "选择导出备份，或上传已有备份进行检查。",
  exporting: "正在加密导出备份，完成后服务会重新连接。",
  inspecting: "正在检查备份密码、完整性和恢复位置…",
  ready_to_restore: "检查通过。确认旧服务器已停止后，才能开始恢复。",
  restoring: "正在恢复数据，请保持服务器运行。页面会自动重新连接。",
  completed: "操作已完成。",
  failed: "操作未完成。请检查密码、备份文件和服务器上的诊断提示。",
  rolled_back: "恢复未通过检查，已恢复操作前的数据。"
};
const backupSteps = {
  preparing: "正在准备", preflight: "正在准备", stopping: "正在暂停服务", stopped: "服务已暂停",
  snapshotting: "正在保存数据", snapshot: "正在保存数据", backing_up: "正在保存数据",
  encrypting: "正在加密备份", exporting: "正在导出备份", uploading: "正在上传备份",
  decrypting: "正在解密检查", validating: "正在检查完整性和恢复位置",
  restoring: "正在恢复数据", starting: "正在启动服务", checking: "正在检查服务",
  rolling_back: "正在恢复操作前的数据", completed: "已完成"
};
const backupErrors = {
  invalid_password: "密码不符合要求，至少需要 12 个字符。",
  decryption_failed: "密码不正确，或备份文件已损坏。当前数据没有被替换。",
  authentication_failed: "密码不正确，或备份文件已损坏。当前数据没有被替换。",
  version_mismatch: "组件版本与备份不一致，请安装匹配版本后再导入。",
  incompatible_components: "组件版本与备份不一致，请安装匹配版本后再导入。",
  insufficient_space: "磁盘空间不足，请腾出空间后再试。",
  insufficient_backup_space: "磁盘空间不足，请腾出空间后再试。",
  unsupported_mode: "当前为外部 AstrBot 附加安装，暂不支持完整迁移备份。",
  expired_plan: "恢复检查已过期，请重新上传检查。",
  restore_plan_expired: "恢复检查已过期，请重新上传检查。",
  updater_restart_failed: "数据恢复已结束，但更新服务需要通过启动脚本重新启动。"
};
let backupState = { enabled: false, phase: "idle", active: false, backups: [] };
let backupToken = "";
let backupLoaded = false;
let backupLoading = false;
let backupPosting = false;
let backupReconnect = false;
let backupNotice = "";
let backupTab = "export";
let backupTimer;
let backupAbort;
let backupUploadProgress = null;
let backupSeenPlan = "";
let backupDismissedPlan = "";
let backupPendingRequest = null;
let backupConfirming = false;
let backupSuppressResult = false;

function backupIsActive() {
  return backupState.active === true || backupActivePhases.has(backupState.phase);
}

function backupId(value) {
  return typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
}

function backupRequestId(value) {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value) ? value : "";
}

function beginBackupRequest(action) {
  backupPendingRequest = { id: "", previous: backupRequestId(backupState.request_id), started: Date.now(),
    phase: { export: "exporting", inspect: "inspecting", restore: "restoring" }[action] };
  backupSuppressResult = true;
}

function backupPasswordValid(password, creating = false) {
  return typeof password === "string" && password.length > 0
    && (!creating || [...password].length >= 12)
    && new TextEncoder().encode(password).length <= 1024;
}

function backupSize(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "—";
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KiB";
  if (value < 1024 * 1024 * 1024) return (value / (1024 * 1024)).toFixed(1) + " MiB";
  return (value / (1024 * 1024 * 1024)).toFixed(2) + " GiB";
}

function backupVersion(value) {
  return typeof value === "string" && /^[A-Za-z0-9._+-]{1,100}$/.test(value) ? value : "—";
}

function validBackupPlan(plan) {
  return plan && typeof plan === "object" && backupId(plan.plan_id)
    && backupVersion(plan.version) !== "—" && plan.path_check === "passed"
    && Number.isSafeInteger(plan.files) && plan.files >= 0
    && Number.isSafeInteger(plan.bytes) && plan.bytes >= 0
    && typeof plan.target_has_data === "boolean"
    && plan.components && typeof plan.components === "object"
    && Object.keys(plan.components).length === names.length
    && names.every(name => backupVersion(plan.components[name]) !== "—");
}

function backupDownloadLink(id) {
  return backupId(id) && backupToken
    ? "/bundle-backup/download/" + id + "?token=" + encodeURIComponent(backupToken) : "";
}

function clearBackupSecrets() {
  for (const id of ["password", "password-confirm", "import-password"]) backupElement(id).value = "";
  backupElement("show-password").checked = false;
  backupElement("password").type = "password";
  backupElement("password-confirm").type = "password";
}

function selectBackupTab(name, focus = false) {
  if (!backupTabs.includes(name)) return;
  backupTab = name;
  for (const key of backupTabs) {
    const selected = key === name;
    const tab = backupElement("tab-" + key);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    backupElement("panel-" + key).hidden = !selected;
    if (selected && focus) tab.focus();
  }
}

function renderBackup() {
  const active = backupIsActive();
  const blocked = !backupLoaded || backupState.enabled !== true || !backupToken
    || backupState.recovery_required === true || backupReconnect || updateIsActive();
  const busy = active || backupPosting || backupLoading;
  const plan = validBackupPlan(backupState.plan) && backupState.phase === "ready_to_restore"
    && backupState.plan.plan_id !== backupDismissedPlan ? backupState.plan : null;
  let message = backupMessages[backupState.phase] || "请重新打开窗口，检查备份状态。";
  if (!backupLoaded) message = "正在连接备份服务…";
  else if (backupState.enabled !== true) message = "备份服务尚未启动。请使用新版启动器启动组合包，或查看服务器部署说明。";
  else if (backupState.recovery_required === true) message = "上次恢复尚未完成。请先按服务器上的诊断提示完成恢复，再进行其他操作。";
  else if (updateIsActive()) message = "组合包正在更新，请等待更新完成后再备份或恢复。";
  else if (backupReconnect) message = "正在重新连接备份服务，不会重复提交操作。";
  else if (backupConfirming) message = "正在确认操作状态，不会重复提交。请保持服务器运行。";
  else if (backupUploadProgress !== null) message = "正在上传备份… " + backupUploadProgress + "%";
  else if (Object.hasOwn(backupErrors, backupState.error)) message = backupErrors[backupState.error];
  else if (backupState.phase === "ready_to_restore" && !validBackupPlan(backupState.plan)) message = "备份检查结果不完整，无法开始恢复。请重新上传检查。";
  backupElement("message").textContent = message;
  backupElement("status").setAttribute("aria-busy", String(busy));
  backupElement("label").textContent = active || backupPosting ? "备份操作进行中" : "备份与恢复";
  const step = backupSteps[backupState.step] || "";
  backupElement("step").hidden = !active || !step || backupUploadProgress !== null;
  backupElement("step").textContent = step;
  backupElement("progress").hidden = !active && !backupPosting;
  if (backupUploadProgress !== null) {
    backupElement("progress").max = 100;
    backupElement("progress").value = backupUploadProgress;
  } else backupElement("progress").removeAttribute("value");
  backupElement("error").textContent = backupNotice;
  backupElement("error").hidden = !backupNotice;
  const password = backupElement("password").value;
  backupElement("export").disabled = blocked || busy || !backupPasswordValid(password, true)
    || password !== backupElement("password-confirm").value;
  backupElement("generate").disabled = blocked || busy;
  const file = backupElement("file").files[0];
  backupElement("inspect").disabled = blocked || busy || !file || !file.size
    || !file.name.toLowerCase().endsWith(".aqbackup") || !backupPasswordValid(backupElement("import-password").value, true);
  backupElement("destination").disabled = busy;
  for (const id of ["password", "password-confirm", "import-password", "file"]) backupElement(id).disabled = busy;
  const result = backupState.phase === "completed" && !active && !backupSuppressResult ? backupState.result : null;
  const downloadHref = result?.kind === "export" ? backupDownloadLink(result.backup_id) : "";
  backupElement("export-result").hidden = !downloadHref;
  backupElement("download").removeAttribute("href");
  if (downloadHref) backupElement("download").href = downloadHref;
  backupElement("restore-result").hidden = result?.kind !== "restore";
  backupElement("import-form").hidden = Boolean(plan) || backupState.phase === "restoring" || result?.kind === "restore";
  backupElement("plan").hidden = !plan;
  if (plan) {
    if (backupSeenPlan !== plan.plan_id) {
      backupSeenPlan = plan.plan_id;
      backupElement("source-stopped").checked = false;
      backupElement("overwrite").checked = false;
    }
    backupElement("plan-version").textContent = plan.version;
    backupElement("plan-size").textContent = plan.files.toLocaleString("zh-CN") + " 个文件 · " + backupSize(plan.bytes);
    const components = backupElement("components");
    components.replaceChildren();
    for (const name of names) {
      const row = document.createElement("div");
      const label = document.createElement("dt");
      const value = document.createElement("dd");
      label.textContent = { astrbot: "AstrBot", qwenpaw: "QwenPaw", napcat: "NapCat" }[name];
      value.textContent = plan.components[name];
      row.append(label, value); components.append(row);
    }
    const warnings = backupElement("warnings");
    warnings.replaceChildren();
    for (const warning of (Array.isArray(plan.warnings) ? plan.warnings : []).slice(0, 12)) {
      if (typeof warning !== "string" || !warning.trim()) continue;
      const item = document.createElement("li");
      item.textContent = cleanUpdateMessage(warning).slice(0, 300);
      warnings.append(item);
    }
    warnings.hidden = !warnings.children.length;
    backupElement("overwrite-row").hidden = !plan.target_has_data;
  }
  backupElement("restore").disabled = blocked || busy || !plan
    || !backupElement("source-stopped").checked
    || (plan.target_has_data && !backupElement("overwrite").checked);
  backupElement("source-stopped").disabled = busy;
  backupElement("overwrite").disabled = busy;
  const list = backupElement("list");
  list.replaceChildren();
  for (const item of (Array.isArray(backupState.backups) ? backupState.backups : []).slice(0, 100)) {
    if (!item || !backupId(item.id)) continue;
    const row = document.createElement("li");
    const detail = document.createElement("div");
    const filename = document.createElement("strong");
    filename.textContent = typeof item.filename === "string" && /^[^\\/\x00-\x1f]{1,160}\.aqbackup$/.test(item.filename)
      ? item.filename : "加密备份.aqbackup";
    const meta = document.createElement("span");
    const created = typeof item.created === "number" || (typeof item.created === "string" && /^\d{9,13}(?:\.\d+)?$/.test(item.created)) ? Number(item.created) : null;
    const date = created !== null && Number.isFinite(created) ? new Date(created < 1e12 ? created * 1000 : created)
      : typeof item.created === "string" && item.created.length <= 40 ? new Date(item.created) : null;
    meta.textContent = [date && Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "日期未知", backupSize(item.size), "v" + backupVersion(item.version)].join(" · ");
    detail.append(filename, meta); row.append(detail);
    const href = !blocked && !active && !backupPosting ? backupDownloadLink(item.id) : "";
    if (href) {
      const link = document.createElement("a"); link.textContent = "下载"; link.href = href;
      link.setAttribute("aria-label", "下载 " + filename.textContent); row.append(link);
    }
    list.append(row);
  }
  backupElement("empty").hidden = list.children.length > 0;
}

function scheduleBackupPoll() {
  clearTimeout(backupTimer);
  if (backupDialog.open || backupIsActive() || backupPosting) backupTimer = setTimeout(() => void readBackupStatus(), 2000);
}

async function readBackupStatus() {
  if (backupLoading || backupPosting) return;
  backupLoading = true;
  backupAbort = new AbortController();
  const abort = backupAbort;
  const timeout = setTimeout(() => abort.abort(), 8000);
  try {
    const response = await fetch("/bundle-backup", { cache: "no-store", signal: abort.signal });
    if (!response.ok) throw new Error("Unavailable");
    const state = await response.json();
    if (!state || typeof state.enabled !== "boolean" || !Object.hasOwn(backupMessages, state.phase)) throw new Error("Invalid state");
    if (backupPendingRequest) {
      const currentId = backupRequestId(state.request_id);
      const matching = backupPendingRequest.id ? currentId === backupPendingRequest.id
        : currentId && currentId !== backupPendingRequest.previous;
      if (matching) {
        backupPendingRequest = null; backupConfirming = false; backupSuppressResult = false; backupNotice = "";
      } else if (Date.now() - backupPendingRequest.started < 130000 || state.active === true || state.recovery_required === true) {
        Object.assign(state, { phase: backupPendingRequest.phase, active: true, plan: null, result: null });
        backupConfirming = true;
      } else {
        // The signed request lifetime has passed. An unacknowledged request is
        // never retried, and an older completed operation cannot look successful.
        backupPendingRequest = null; backupConfirming = false;
        Object.assign(state, { phase: "idle", active: false, plan: null, result: null });
        backupNotice = "未确认请求已被接收。请检查服务器状态后再决定是否重试；系统没有重复提交。";
      }
    }
    backupState = state;
    backupToken = typeof state.csrf_token === "string" && /^[A-Za-z0-9_-]{16,256}$/.test(state.csrf_token) ? state.csrf_token : "";
    backupLoaded = true;
    backupReconnect = false;
  } catch {
    if (!abort.signal.aborted || backupDialog.open || backupIsActive()) backupReconnect = true;
    if (!backupLoaded) { backupLoaded = true; backupState.enabled = false; }
  } finally {
    clearTimeout(timeout);
    if (backupAbort === abort) backupAbort = null;
    backupLoading = false;
    renderBackup();
    scheduleBackupPoll();
  }
}

async function backupPost(action, body) {
  const response = await fetch("/bundle-backup/" + action, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Bundle-Backup-Token": backupToken },
    body: JSON.stringify(body), signal: AbortSignal.timeout(20000)
  });
  if (response.status !== 202) {
    backupPendingRequest = null; backupConfirming = false;
    throw new Error("Request not accepted");
  }
  const accepted = await response.json();
  if (backupPendingRequest) backupPendingRequest.id = backupRequestId(accepted?.request_id);
}

function canPostBackup() {
  return !backupPosting && !backupLoading && !backupReconnect && !backupPendingRequest && !backupIsActive() && !updateIsActive()
    && backupState.enabled === true && backupState.recovery_required !== true && backupToken;
}

async function submitBackupExport(event) {
  event.preventDefault();
  if (!canPostBackup()) return;
  let password = backupElement("password").value;
  if (!backupPasswordValid(password, true) || password !== backupElement("password-confirm").value) return;
  const destination = backupElement("destination").value;
  if (!["server", "download"].includes(destination)) return;
  clearBackupSecrets();
  beginBackupRequest("export");
  backupPosting = true; backupNotice = ""; renderBackup();
  try {
    await backupPost("export", { password, destination });
    backupState = { ...backupState, phase: "exporting", active: true, plan: null, result: null };
  } catch {
    backupNotice = "导出请求的结果尚未确认。请查看操作状态，不会自动重复导出。";
    backupReconnect = true;
  } finally {
    password = ""; backupPosting = false; renderBackup(); await readBackupStatus();
  }
}

function uploadBackup(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/bundle-backup/upload");
    request.setRequestHeader("X-Bundle-Backup-Token", backupToken);
    request.responseType = "json";
    request.timeout = 30 * 60 * 1000;
    request.upload.onprogress = event => {
      if (event.lengthComputable) { backupUploadProgress = Math.max(0, Math.min(100, Math.round(event.loaded * 100 / event.total))); renderBackup(); }
    };
    request.onload = () => {
      const body = request.response;
      if (request.status >= 200 && request.status < 300 && backupId(body?.upload_id)) resolve(body.upload_id);
      else reject(new Error("Upload not accepted"));
    };
    request.onerror = request.ontimeout = request.onabort = () => reject(new Error("Upload unavailable"));
    const data = new FormData(); data.append("file", file); request.send(data);
  });
}

async function submitBackupImport(event) {
  event.preventDefault();
  if (!canPostBackup()) return;
  const file = backupElement("file").files[0];
  let password = backupElement("import-password").value;
  if (!file || !file.size || !file.name.toLowerCase().endsWith(".aqbackup") || !backupPasswordValid(password, true)) return;
  clearBackupSecrets();
  backupState = { ...backupState, plan: null, result: null };
  backupSeenPlan = ""; backupDismissedPlan = ""; backupPosting = true; backupUploadProgress = 0; backupNotice = ""; renderBackup();
  try {
    const uploadId = await uploadBackup(file);
    backupUploadProgress = null;
    // A completed upload can rotate the token; read-only refresh before inspect.
    const response = await fetch("/bundle-backup", { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error("Status unavailable");
    const state = await response.json();
    if (typeof state.csrf_token !== "string" || !/^[A-Za-z0-9_-]{16,256}$/.test(state.csrf_token)) throw new Error("Invalid token");
    backupToken = state.csrf_token;
    beginBackupRequest("inspect");
    await backupPost("inspect", { upload_id: uploadId, password });
    backupState = { ...backupState, phase: "inspecting", active: true };
  } catch {
    backupNotice = "上传或检查未完成。请查看操作状态，确认备份文件和密码后再试。不会自动开始恢复。";
    backupReconnect = true;
  } finally {
    password = ""; backupUploadProgress = null; backupPosting = false; renderBackup(); await readBackupStatus();
  }
}

async function submitBackupRestore() {
  if (!canPostBackup() || !validBackupPlan(backupState.plan) || backupState.phase !== "ready_to_restore") return;
  const plan = backupState.plan;
  if (!backupElement("source-stopped").checked || (plan.target_has_data && !backupElement("overwrite").checked)) return;
  beginBackupRequest("restore");
  backupPosting = true; backupNotice = ""; renderBackup();
  try {
    await backupPost("restore", { plan_id: plan.plan_id, confirm_source_stopped: true, overwrite: backupElement("overwrite").checked });
    backupState = { ...backupState, phase: "restoring", active: true };
  } catch {
    backupNotice = "恢复请求的结果尚未确认，正在重新连接；不会自动重复提交恢复。";
    backupReconnect = true;
  } finally {
    backupPosting = false; renderBackup(); await readBackupStatus();
  }
}

for (const name of backupTabs) {
  const tab = backupElement("tab-" + name);
  tab.addEventListener("click", () => selectBackupTab(name));
  tab.addEventListener("keydown", event => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let index = backupTabs.indexOf(backupTab);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = backupTabs.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + backupTabs.length) % backupTabs.length;
    selectBackupTab(backupTabs[index], true);
  });
}
backupElement("toggle").addEventListener("click", () => {
  if (!backupDialog.open) backupDialog.showModal();
  void readBackupStatus();
});
function clearBackupDialogInputs() {
  clearBackupSecrets();
  backupElement("file").value = "";
}

function closeBackupDialog() {
  // Native dialog close events can be deferred. Remove secrets before hiding
  // the dialog so Escape, the close button and backdrop have the same guarantee.
  clearBackupDialogInputs();
  backupDialog.close();
}

backupElement("close").addEventListener("click", closeBackupDialog);
backupDialog.addEventListener("cancel", event => {
  event.preventDefault();
  closeBackupDialog();
});
backupDialog.addEventListener("close", () => {
  clearBackupDialogInputs();
  if (!backupIsActive() && !backupPosting) backupAbort?.abort();
  scheduleBackupPoll();
});
backupDialog.addEventListener("click", event => {
  if (event.target !== backupDialog) return;
  const rect = backupDialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeBackupDialog();
});
for (const id of ["password", "password-confirm", "import-password", "file", "source-stopped", "overwrite"]) {
  backupElement(id).addEventListener(id === "file" || id === "source-stopped" || id === "overwrite" ? "change" : "input", renderBackup);
}
backupElement("destination").addEventListener("change", () => {
  backupElement("destination-help").textContent = backupElement("destination").value === "download"
    ? "导出完成后点击下载，将加密备份保存到这台电脑。密码不会写入备份。"
    : "本地备份也应再复制一份到其他设备，避免与服务器一起丢失。";
});
backupElement("show-password").addEventListener("change", event => {
  for (const id of ["password", "password-confirm"]) backupElement(id).type = event.target.checked ? "text" : "password";
});
backupElement("generate").addEventListener("click", () => {
  if (!canPostBackup()) return;
  const random = crypto.getRandomValues(new Uint8Array(24));
  const password = btoa(String.fromCharCode(...random)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  backupElement("password").value = password; backupElement("password-confirm").value = password;
  backupElement("show-password").checked = true;
  backupElement("password").type = "text"; backupElement("password-confirm").type = "text";
  backupElement("password").focus(); backupElement("password").select(); renderBackup();
});
backupElement("export-form").addEventListener("submit", event => void submitBackupExport(event));
backupElement("import-form").addEventListener("submit", event => void submitBackupImport(event));
backupElement("restore").addEventListener("click", () => void submitBackupRestore());
backupElement("change-file").addEventListener("click", () => {
  if (!canPostBackup()) return;
  backupDismissedPlan = backupState.plan?.plan_id || "";
  backupElement("file").value = "";
  clearBackupSecrets(); renderBackup(); backupElement("file").focus();
});
backupElement("reload").addEventListener("click", () => location.reload());
renderBackup();
void readBackupStatus();
