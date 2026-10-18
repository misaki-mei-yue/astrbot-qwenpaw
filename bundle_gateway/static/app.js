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
