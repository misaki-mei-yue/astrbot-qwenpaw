/* AstrBot's parent bridge supplies dashboard authentication. No tokens here. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const titles = {
    overview: ["运行概览", "连接与运行状态"],
    tasks: ["主动任务", "把事情安排好，让结果主动回来"],
    memory: ["长期记忆", "查看保存下来的经验与上下文"],
    tools: ["工具能力", "了解智能体目前能使用什么"],
  };
  let view = "overview",
    api = null,
    state = null,
    allTools = [],
    loadId = 0,
    memoryReadId = 0;
  let creating = false,
    createUncertain = false,
    createId = "";
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  function notice(message) {
    $("notice").textContent = message;
    $("notice").hidden = !message;
  }
  function errorMessage(error) {
    return error?.message || "暂时无法读取，请稍后刷新。";
  }
  function platform(value) {
    return value === "weixin_oc"
      ? "微信"
      : value === "aiocqhttp"
        ? "QQ"
        : value;
  }
  function routeLabel(route) {
    return `${platform(route.platform)} · ${route.user_id} · ${route.origin}`;
  }
  function date(value) {
    if (!value) return "";
    const time = new Date(value);
    return Number.isNaN(time.getTime()) ? value : time.toLocaleString("zh-CN");
  }
  function empty(target, message) {
    target.replaceChildren(el("p", message));
    target.classList.add("empty");
  }
  function call(endpoint, body) {
    if (!api)
      return Promise.reject(
        new Error("请从 AstrBot 后台的插件页面打开组合工作台。"),
      );
    return body === undefined
      ? api.apiGet("workbench/" + endpoint)
      : api.apiPost("workbench/" + endpoint, body);
  }
  function showOverview(data) {
    state = data;
    const list = $("status-list");
    list.replaceChildren();
    const rows = [
      ["消息桥接", data.bridge_ready ? "已启动" : "未启动", data.bridge_ready],
      [
        "智能体运行",
        data.qwenpaw_ready
          ? "已就绪" + (data.version ? " · " + data.version : "")
          : "尚未就绪",
        data.qwenpaw_ready,
      ],
      [
        "对话模型",
        data.model.configured ? "已配置 · " + data.model.name : "尚未配置",
        data.model.configured,
      ],
      [
        "自动记忆",
        data.failures.memory
          ? "状态不可用"
          : data.memory.has_error
            ? "最近有错误，请检查"
            : data.memory.auto_enabled
              ? "已启用"
              : "未启用",
        !data.failures.memory &&
          data.memory.auto_enabled &&
          !data.memory.has_error,
      ],
      ["正在处理", String(data.active_turns) + " 段对话", true],
    ];
    for (const [label, value, ok] of rows) {
      const row = el("div");
      row.append(el("dt", label), el("dd", value, ok ? "ok" : "warn"));
      list.append(row);
    }
    $("owner-hint").textContent = data.owner_count
      ? `已允许 ${data.owner_count} 个用户。`
      : "尚未允许任何用户。请在桥接插件设置中填写用户 ID；可在聊天中发送 /paw whoami 查询。";
    const routes = $("routes-list");
    routes.replaceChildren();
    routes.classList.remove("empty");
    if (!data.routes.length)
      empty(
        routes,
        "还没有获准的聊天会话。先用微信或 QQ 与机器人聊一句，再刷新这里。",
      );
    for (const route of data.routes) {
      const row = el("div", undefined, "row");
      const main = el("div", undefined, "row-main");
      main.append(
        el("strong", platform(route.platform) + " · " + route.user_id),
        el("p", route.origin),
      );
      row.append(main, el("span", "已建立", "badge"));
      routes.append(row);
    }
    const select = $("task-form").elements.session_id,
      old = select.value;
    select.replaceChildren(new Option("选择接收结果的会话", ""));
    for (const route of data.routes)
      select.add(new Option(routeLabel(route), route.session_id));
    if (data.routes.some((r) => r.session_id === old)) select.value = old;
    const failures = Object.values(data.failures);
    if (failures.length) notice([...new Set(failures)].join("\n"));
  }
  function scheduleText(job) {
    const s = job.schedule;
    if (s.type === "once") return "单次 · " + date(s.run_at);
    const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|mon-fri)$/.exec(s.cron || "");
    const zone =
      s.timezone === "Asia/Shanghai" ? "北京时间" : s.timezone || "UTC";
    return match
      ? `${match[3] === "*" ? "每天" : "周一至周五"} ${match[2].padStart(2, "0")}:${match[1].padStart(2, "0")}（${zone}）`
      : `自定义定时计划（${zone}）`;
  }
  function renderTasks(data) {
    const target = $("task-list");
    target.replaceChildren();
    target.classList.remove("empty");
    if (!data.items.length) {
      empty(
        target,
        "还没有主动任务。安排一个提醒，或让智能体定时帮你整理信息。",
      );
      return;
    }
    for (const job of data.items) {
      const row = el("div", undefined, "row"),
        main = el("div", undefined, "row-main"),
        side = el("div", undefined, "row-side");
      main.append(
        el("strong", job.name),
        el(
          "p",
          `${job.kind === "text" ? "提醒" : "智能任务"} · ${scheduleText(job)}`,
        ),
      );
      const route = state?.routes.find((r) => r.session_id === job.session_id);
      if (route) main.append(el("p", routeLabel(route)));
      const control = el("button", job.enabled ? "暂停" : "启用", "secondary");
      control.addEventListener("click", async () => {
        control.disabled = true;
        try {
          await call("tasks/control", {
            id: job.id,
            action: job.enabled ? "pause" : "resume",
          });
          await refresh();
        } catch (error) {
          notice(errorMessage(error));
        } finally {
          control.disabled = false;
        }
      });
      side.append(
        el(
          "span",
          job.enabled ? "已启用" : "已暂停",
          job.enabled ? "badge" : "badge off",
        ),
        control,
      );
      row.append(main, side);
      target.append(row);
    }
  }
  function renderMemory(data) {
    const target = $("memory-list");
    target.replaceChildren();
    if (!data.items.length) {
      target.append(
        el(
          "p",
          "暂时还没有保存的记忆。完成对话并等待自动记忆整理后，再来查看。",
        ),
      );
      return;
    }
    for (const item of data.items) {
      const button = el("button", item.name, "memory-item");
      button.append(
        el(
          "small",
          `${item.section === "daily" ? "日常记忆" : "整理摘要"} · ${date(item.modified)}`,
        ),
      );
      button.addEventListener("click", async () => {
        const id = ++memoryReadId;
        document
          .querySelectorAll(".memory-item")
          .forEach((n) => n.classList.remove("selected"));
        button.classList.add("selected");
        $("memory-title").textContent = item.name;
        $("memory-meta").textContent = "正在读取…";
        $("memory-content").textContent = "";
        try {
          const result = await call("memory/read", {
            section: item.section,
            name: item.name,
          });
          if (id !== memoryReadId) return;
          $("memory-content").textContent =
            result.content || "这份记忆暂时没有内容。";
          $("memory-meta").textContent =
            (item.section === "daily" ? "日常记忆" : "整理摘要") +
            " · " +
            date(item.modified);
        } catch (error) {
          if (id !== memoryReadId) return;
          $("memory-meta").textContent = "读取失败";
          $("memory-content").textContent = errorMessage(error);
        }
      });
      target.append(button);
    }
  }
  function renderTools() {
    const target = $("tool-list"),
      query = $("tool-search").value.toLowerCase().trim();
    target.replaceChildren();
    target.classList.remove("empty");
    const tools = allTools.filter((t) =>
      (t.name + " " + t.description).toLowerCase().includes(query),
    );
    if (!tools.length) {
      empty(target, query ? "没有匹配的工具。" : "暂时没有登记的工具。");
      return;
    }
    for (const tool of tools) {
      const row = el("div", undefined, "row"),
        main = el("div", undefined, "row-main");
      main.append(
        el("strong", tool.name, "tool-name"),
        el("p", tool.description),
      );
      row.append(
        main,
        el(
          "span",
          tool.enabled ? "已启用" : "未启用",
          tool.enabled ? "badge" : "badge off",
        ),
      );
      target.append(row);
    }
  }
  async function refresh() {
    const id = ++loadId,
      current = view;
    $("refresh").disabled = true;
    notice("");
    try {
      if (current === "overview") {
        const data = await call("status");
        if (id === loadId) showOverview(data);
      } else if (current === "tasks") {
        const results = await Promise.allSettled([
          call("status"),
          call("tasks"),
        ]);
        if (id !== loadId) return;
        if (results[0].status === "fulfilled") showOverview(results[0].value);
        if (results[1].status === "fulfilled") renderTasks(results[1].value);
        else throw results[1].reason;
      } else if (current === "memory") {
        const data = await call("memory");
        if (id === loadId) renderMemory(data);
      } else {
        const data = await call("tools");
        if (id === loadId) {
          allTools = data.items;
          renderTools();
        }
      }
    } catch (error) {
      if (id === loadId) notice(errorMessage(error));
    } finally {
      if (id === loadId) $("refresh").disabled = false;
    }
  }
  function navigate(next) {
    view = next;
    document
      .querySelectorAll(".view")
      .forEach((n) => (n.hidden = n.id !== "view-" + next));
    document.querySelectorAll(".nav-button").forEach((n) => {
      const selected = n.dataset.view === next;
      n.classList.toggle("selected", selected);
      if (selected) n.setAttribute("aria-current", "page");
      else n.removeAttribute("aria-current");
    });
    $("page-title").textContent = titles[next][0];
    $("page-caption").textContent = titles[next][1];
    refresh();
  }
  function frequency() {
    const once = $("task-form").elements.frequency.value === "once";
    $("once-time").hidden = !once;
    $("recurring-time").hidden = once;
    $("task-form").elements.run_at.required = once;
    $("task-form").elements.time.required = !once;
  }
  function newTask() {
    if (creating) return;
    $("task-form").reset();
    createId = crypto.randomUUID().replaceAll("-", "");
    createUncertain = false;
    $("task-form").querySelector('[type="submit"]').disabled = false;
    $("task-feedback").textContent = "";
    frequency();
    $("task-form").hidden = false;
    $("task-form").elements.name.focus();
  }
  $("new-task").addEventListener("click", newTask);
  $("close-task").addEventListener("click", () => {
    if (!creating) $("task-form").hidden = true;
  });
  $("task-form").elements.frequency.addEventListener("change", frequency);
  $("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (creating || createUncertain) return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const body = Object.fromEntries(new FormData(form));
    body.request_id = createId;
    if (body.frequency === "once") {
      const time = new Date(body.run_at);
      if (Number.isNaN(time.getTime()) || time <= new Date()) {
        $("task-feedback").textContent = "请选择未来时间。";
        return;
      }
      body.run_at = time.toISOString();
    }
    creating = true;
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    $("task-feedback").textContent = "正在创建…";
    try {
      await call("tasks/create", body);
      form.hidden = true;
      await refresh();
      notice("任务已创建并启用。结果将发回所选会话。");
    } catch (error) {
      createUncertain = true;
      $("task-feedback").textContent =
        errorMessage(error) +
        " 请刷新任务列表确认；若未创建，可重新点“新建任务”。";
    } finally {
      creating = false;
      if (!createUncertain) submit.disabled = false;
    }
  });
  document
    .querySelectorAll(".nav-button")
    .forEach((n) =>
      n.addEventListener("click", () => navigate(n.dataset.view)),
    );
  document
    .querySelectorAll("[data-go]")
    .forEach((n) => n.addEventListener("click", () => navigate(n.dataset.go)));
  $("refresh").addEventListener("click", refresh);
  $("tool-search").addEventListener("input", renderTools);
  async function init() {
    api = window.AstrBotPluginPage;
    if (!api || window.parent === window) {
      api = null;
      notice("请登录 AstrBot，在桥接插件的页面入口打开“组合工作台”。");
      return;
    }
    try {
      await Promise.race([
        api.ready(),
        new Promise((_, reject) =>
          setTimeout(
            () => reject(new Error("后台连接超时，请返回插件页面重新打开。")),
            10000,
          ),
        ),
      ]);
      await refresh();
    } catch (error) {
      notice(errorMessage(error));
    }
  }
  init();
})();
