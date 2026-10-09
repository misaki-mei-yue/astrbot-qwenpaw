/* Browser checks with synthetic data only. Run after installing Playwright.
   WORKBENCH_BROWSER optionally selects an existing Chromium executable.
   WORKBENCH_SCREENSHOTS optionally saves local review images. */
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const root = path.resolve(
  __dirname,
  "../astrbot_plugin_qwenpaw_bridge/pages/workbench",
);
const sid = "ab_" + "a".repeat(32);
const requests = [];
const tasks = [];
let rejectCreate = false;
const fixture = {
  status: {
    bridge_ready: true,
    qwenpaw_ready: true,
    version: "2.2.1",
    model: { configured: true, name: "local-test-model" },
    memory: {
      status: "idle",
      auto_enabled: true,
      has_error: false,
      reindexing: false,
    },
    owner_count: 1,
    active_turns: 0,
    tool_allowlist_count: 2,
    all_tools_allowed: false,
    routes: [
      {
        session_id: sid,
        user_id: "demo-owner",
        platform: "weixin_oc",
        origin: "demo:FriendMessage:demo-owner",
      },
    ],
    failures: {},
  },
  memory: {
    items: [
      {
        name: "2026-10-09.md",
        section: "daily",
        modified: "2026-10-09T08:00:00Z",
        size: 200,
      },
    ],
  },
  tools: {
    items: [
      {
        name: "browser_use",
        description: "操作浏览器，读取网页与完成交互。",
        enabled: true,
      },
      {
        name: "write_file",
        description: "在工作区内写入文件。",
        enabled: true,
      },
      { name: "web_search", description: "检索公开资料。", enabled: false },
    ],
  },
};
const sdk = `window.AstrBotPluginPage={ready:()=>Promise.resolve({}),apiGet:(p)=>fetch('/fixture/'+p).then(async r=>{let b=await r.json();if(!r.ok)throw Error(b.message);return b}),apiPost:(p,b)=>fetch('/fixture/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(async r=>{let b=await r.json();if(!r.ok)throw Error(b.message);return b})};`;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/") {
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(
      '<!doctype html><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block}</style><iframe title="工作台" src="/index.html"></iframe>',
    );
    return;
  }
  if (url.pathname === "/api/plugin/page/bridge-sdk.js") {
    res.setHeader("Content-Type", "application/javascript");
    res.end(sdk);
    return;
  }
  if (url.pathname.startsWith("/fixture/workbench/")) {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {},
      op = url.pathname.slice("/fixture/workbench/".length);
    requests.push({ op, body });
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    let result;
    if (op === "tasks") result = { items: tasks };
    else if (op === "tasks/create") {
      if (rejectCreate) {
        res.statusCode = 502;
        res.end(
          JSON.stringify({ message: "连接智能体失败，请先刷新任务列表确认。" }),
        );
        return;
      }
      result = {
        id: "test-job",
        name: body.name,
        enabled: true,
        kind: body.kind,
        session_id: body.session_id,
        schedule: {
          type: "cron",
          cron: "0 9 * * *",
          timezone: "Asia/Shanghai",
        },
      };
      tasks.push(result);
    } else if (op === "tasks/control") {
      tasks.find((t) => t.id === body.id).enabled = body.action === "resume";
      result = { ok: true };
    } else if (op === "memory/read")
      result = {
        name: body.name,
        section: body.section,
        content:
          '<img src=x onerror="window.memoryInjected=true">\n用户喜欢简洁的回答。',
      };
    else result = fixture[op] || {};
    res.end(JSON.stringify(result));
    return;
  }
  const name = url.pathname.slice(1);
  if (!["index.html", "app.js", "style.css"].includes(name)) {
    res.statusCode = 404;
    res.end();
    return;
  }
  res.setHeader(
    "Content-Type",
    {
      "index.html": "text/html; charset=utf-8",
      "app.js": "application/javascript",
      "style.css": "text/css",
    }[name],
  );
  res.end(fs.readFileSync(path.join(root, name)));
});
async function main() {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.WORKBENCH_BROWSER
      ? { executablePath: process.env.WORKBENCH_BROWSER }
      : {}),
  });
  const failures = [];
  try {
    const page = await browser.newPage({
      viewport: { width: 1400, height: 960 },
    });
    page.on("pageerror", (e) => failures.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const frame = page.frameLocator("iframe");
    await frame
      .locator("#status-list")
      .getByText("已配置 · local-test-model")
      .waitFor();
    assert.equal(await frame.locator("#notice").isVisible(), false);
    const screenshotDir = process.env.WORKBENCH_SCREENSHOTS;
    if (screenshotDir) {
      fs.mkdirSync(screenshotDir, { recursive: true });
      await page.screenshot({
        path: path.join(screenshotDir, "workbench-desktop.png"),
        fullPage: true,
      });
    }
    await frame.getByRole("button", { name: "主动任务", exact: true }).click();
    await frame
      .locator("#task-list")
      .getByText(/还没有主动任务/)
      .waitFor();
    await frame.getByRole("button", { name: "新建任务", exact: true }).click();
    await frame.getByLabel("任务名称", { exact: true }).fill("阅读摘要");
    await frame.getByLabel("结果发送到").selectOption(sid);
    await frame
      .getByLabel("需要做什么")
      .fill("整理今天的三条技术新闻，并附上来源。");
    await frame.getByRole("button", { name: "创建并启用" }).click();
    await frame
      .locator("#task-list")
      .getByText("阅读摘要", { exact: true })
      .waitFor();
    const created = requests.filter((r) => r.op === "tasks/create");
    assert.equal(created.length, 1);
    assert.match(created[0].body.request_id, /^[a-f0-9]{32}$/);
    assert.equal(created[0].body.session_id, sid);
    assert.equal(created[0].body.kind, "agent");
    await frame.getByRole("button", { name: "暂停", exact: true }).click();
    await frame
      .locator("#task-list")
      .getByText("已暂停", { exact: true })
      .waitFor();
    await frame.getByRole("button", { name: "启用", exact: true }).click();
    await frame
      .locator("#task-list")
      .getByText("已启用", { exact: true })
      .waitFor();
    assert.deepEqual(
      requests
        .filter((r) => r.op === "tasks/control")
        .map((r) => r.body.action),
      ["pause", "resume"],
    );
    if (screenshotDir)
      await page.screenshot({
        path: path.join(screenshotDir, "workbench-tasks.png"),
        fullPage: true,
      });
    rejectCreate = true;
    await frame.getByRole("button", { name: "新建任务", exact: true }).click();
    await frame.getByLabel("任务名称", { exact: true }).fill("不确定请求");
    await frame.getByLabel("结果发送到").selectOption(sid);
    await frame.getByLabel("需要做什么").fill("仅用于验证超时后不重复提交。");
    await frame.getByRole("button", { name: "创建并启用" }).click();
    await frame
      .locator("#task-feedback")
      .getByText(/请刷新任务列表确认/)
      .waitFor();
    assert.equal(
      await frame.getByRole("button", { name: "创建并启用" }).isDisabled(),
      true,
    );
    assert.equal(requests.filter((r) => r.op === "tasks/create").length, 2);
    await frame.getByRole("button", { name: "长期记忆", exact: true }).click();
    await frame.locator(".memory-item").first().click();
    await frame
      .locator("#memory-content")
      .getByText(/用户喜欢简洁的回答/)
      .waitFor();
    assert.equal(await frame.locator("#memory-content img").count(), 0);
    const child = page.frames().find((f) => f.url().endsWith("index.html"));
    assert.equal(await child.evaluate(() => window.memoryInjected), undefined);
    await frame.getByRole("button", { name: "工具能力", exact: true }).click();
    await frame
      .locator("#tool-list")
      .getByText("browser_use", { exact: true })
      .waitFor();
    await frame.getByLabel("查找工具").fill("浏览器");
    assert.equal(await frame.locator("#tool-list .row").count(), 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await frame.getByRole("button", { name: "运行概览", exact: true }).click();
    await frame
      .locator("#status-list")
      .getByText("已配置 · local-test-model")
      .waitFor();
    assert.ok(
      await child.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      "mobile layout must not overflow",
    );
    if (screenshotDir)
      await page.screenshot({
        path: path.join(screenshotDir, "workbench-mobile.png"),
        fullPage: true,
      });
    await frame.getByRole("button", { name: "主动任务", exact: true }).click();
    await frame.getByRole("button", { name: "新建任务", exact: true }).click();
    await frame.getByLabel("执行频率").selectOption("once");
    assert.equal(await frame.locator("#once-time").isVisible(), true);
    assert.equal(await frame.locator("#recurring-time").isVisible(), false);
    assert.ok(
      await child.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      "mobile form must not overflow",
    );
    assert.deepEqual(failures, []);
    await page.goto(`http://127.0.0.1:${server.address().port}/index.html`);
    await page
      .locator("#notice")
      .getByText(/请登录 AstrBot/)
      .waitFor();
    console.log(
      "PASS: workbench desktop/mobile, tasks, pause/resume, uncertain-create guard, memory text safety, search, standalone guard.",
    );
  } finally {
    await browser.close();
  }
}
main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(() => server.close());
