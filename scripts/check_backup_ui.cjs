// Browser-only fixtures: no real accounts, Docker operations, or model calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { chromium } = require("playwright");

(async () => {
  const staticRoot = path.join(__dirname, "..", "bundle_gateway", "static");
  const server = http.createServer((request, response) => {
    if (/^(astrbot|qwenpaw|napcat)\.localhost:/.test(request.headers.host || "")) {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end('<!doctype html><html lang="zh-CN"><title>原生后台测试替身</title><label>未保存设置<input id="draft"></label></html>');
      return;
    }
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === "/bundle-health") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ services: { astrbot: { ready: true }, qwenpaw: { ready: true }, napcat: { ready: true } } }));
      return;
    }
    if (pathname === "/bundle-update") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ enabled: false, phase: "idle", active: false }));
      return;
    }
    const file = { "/": "index.html", "/app.js": "app.js", "/style.css": "style.css" }[pathname];
    if (!file) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, { "Content-Type": { "index.html": "text/html; charset=utf-8", "app.js": "application/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8" }[file] });
    response.end(fs.readFileSync(path.join(staticRoot, file)));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = "http://localhost:" + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(process.env.BUNDLE_TEST_BROWSER ? { executablePath: process.env.BUNDLE_TEST_BROWSER } : {}), args: ["--no-proxy-server"] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    let revision = 1;
    const token = () => "backup-fixture-token-" + revision;
    let state = { enabled: false, phase: "idle", active: false, backups: [] };
    const mutations = [];
    let getFailures = 0;
    let abortRestore = false;
    let getCount = 0;
    let staleReads = 0;
    let staleState = null;
    const plan = {
      plan_id: "c".repeat(32), version: "0.7.0-alpha", components: { astrbot: "4.25.1", qwenpaw: "2.2.1", napcat: "4.18.33" },
      files: 128, bytes: 1024 * 1024 * 8, target_has_data: true, path_check: "passed",
      warnings: ["QQ 可能需要重新扫码。", "<img src=x onerror=alert(1)>", "旧位置 E:\\PrivatePerson\\runtime 将不会直接使用。"]
    };
    await page.route(origin + "/bundle-backup**", async route => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "GET") {
        if (pathname.startsWith("/bundle-backup/download/")) {
          assert.equal(new URL(request.url()).searchParams.get("token"), token());
          return route.fulfill({ status: 200, headers: { "Content-Disposition": 'attachment; filename="fixture.aqbackup"' }, contentType: "application/octet-stream", body: "encrypted-fixture-only" });
        }
        getCount++;
        if (getFailures > 0) { getFailures--; return route.abort("connectionrefused"); }
        if (staleReads > 0) {
          staleReads--;
          return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...staleState, csrf_token: token() }) });
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...state, csrf_token: token() }) });
      }
      assert.equal(request.headers()["x-bundle-backup-token"], token());
      assert.equal(request.headers().origin, origin);
      if (pathname.endsWith("/upload")) {
        assert.match(request.headers()["content-type"], /^multipart\/form-data; boundary=/);
        const raw = request.postDataBuffer().toString("utf8");
        assert.match(raw, /name="file"; filename="fixture\.aqbackup"/);
        assert.equal((raw.match(/Content-Disposition: form-data/g) || []).length, 1);
        assert.ok(!raw.includes("FixturePassword"), "Passwords must not enter multipart upload content");
        mutations.push({ pathname }); revision++;
        return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ upload_id: "b".repeat(32) }) });
      }
      assert.equal(request.headers()["content-type"], "application/json");
      const body = request.postDataJSON();
      mutations.push({ pathname, body }); revision++;
      const requestId = randomUUID();
      if (pathname.endsWith("/export")) {
        assert.deepEqual(body, { password: "FixturePassword-not-real", destination: "download" });
        staleState = { ...state, phase: "completed", active: false, request_id: "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", result: { kind: "export", backup_id: "e".repeat(32) } };
        staleReads = 1;
        state = { ...state, request_id: requestId, phase: "exporting", active: true, step: "encrypting", result: null };
      } else if (pathname.endsWith("/inspect")) {
        assert.deepEqual(body, { upload_id: "b".repeat(32), password: "FixturePassword-not-real" });
        state = { ...state, request_id: requestId, phase: "ready_to_restore", active: false, plan };
      } else if (pathname.endsWith("/restore")) {
        assert.deepEqual(body, { plan_id: plan.plan_id, confirm_source_stopped: true, overwrite: true });
        state = { ...state, request_id: requestId, phase: "restoring", active: true, step: "restoring" };
        getFailures = 2;
        if (abortRestore) return route.abort("connectionreset");
      } else throw new Error("Unexpected backup mutation");
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ request_id: requestId }) });
    });
    await page.goto(origin + "/#astrbot");
    await page.locator('#panel-astrbot:not([hidden]) iframe').waitFor();
    const native = name => page.frameLocator(`#panel-${name} iframe`);
    await native("astrbot").locator("#draft").fill("这是尚未保存的原生后台设置");
    await page.locator('[data-app="qwenpaw"]').click();
    await native("qwenpaw").locator("#draft").fill("记忆后台中的草稿");
    await page.locator("#backup-toggle").click();
    await page.locator("#backup-message").filter({ hasText: "新版启动器" }).waitFor();
    assert.equal(await page.locator("#backup-dialog").getAttribute("aria-labelledby"), "backup-title");
    assert.equal(await page.locator("#backup-export").isDisabled(), true);
    assert.equal(mutations.length, 0, "Opening is read-only");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#backup-dialog").evaluate(element => element.open), false);
    assert.equal(await page.locator("#backup-toggle").evaluate(element => document.activeElement === element), true);
    state.enabled = true;
    await page.locator("#backup-toggle").click();
    await page.locator("#backup-generate:not([disabled])").waitFor();
    await page.locator("#backup-tab-export").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#backup-tab-import").getAttribute("aria-selected"), "true");
    await page.keyboard.press("End");
    assert.equal(await page.locator("#backup-tab-list").getAttribute("aria-selected"), "true");
    await page.keyboard.press("Home");
    assert.equal(await page.locator("#backup-tab-export").getAttribute("aria-selected"), "true");
    await page.getByLabel("备份密码", { exact: true }).first().fill("short");
    await page.getByLabel("再次输入密码", { exact: true }).fill("short");
    assert.equal(await page.locator("#backup-export").isDisabled(), true);
    await page.locator("#backup-password").fill("汉".repeat(400));
    await page.locator("#backup-password-confirm").fill("汉".repeat(400));
    assert.equal(await page.locator("#backup-export").isDisabled(), true, "UTF-8 password size is bounded");
    await page.locator("#backup-generate").click();
    const generated = await page.locator("#backup-password").inputValue();
    assert.match(generated, /^[A-Za-z0-9_-]{32}$/);
    assert.equal(await page.locator("#backup-password-confirm").inputValue(), generated);
    assert.equal(await page.locator("#backup-password").getAttribute("type"), "text");
    await page.locator("#backup-password-confirm").fill(generated + "mismatch");
    assert.equal(await page.locator("#backup-export").isDisabled(), true);
    await page.locator("#backup-password").fill("FixturePassword-not-real");
    await page.locator("#backup-password-confirm").fill("FixturePassword-not-real");
    await page.locator("#backup-destination").selectOption("download");
    await page.locator("#backup-export:not([disabled])").click();
    assert.equal(await page.locator("#backup-password").inputValue(), "");
    assert.equal(await page.locator("#backup-password-confirm").inputValue(), "");
    await page.locator("#backup-message").filter({ hasText: "确认操作状态" }).waitFor();
    assert.equal(await page.locator("#backup-export-result").isVisible(), false, "An old completed job must not be shown as the newly accepted export");
    assert.equal(await page.locator("#backup-export").isDisabled(), true);
    await page.locator("#backup-message").filter({ hasText: "正在加密" }).waitFor();
    assert.equal(mutations.length, 1);
    assert.equal(await page.locator("#backup-inspect").isDisabled(), true);
    state = { ...state, phase: "completed", active: false, result: { kind: "export", backup_id: "a".repeat(32), download_url: "https://attacker.invalid/private" }, backups: [
      { id: "a".repeat(32), filename: "fixture-20261019.aqbackup", size: 2048, created: "1792396800.25", version: "0.7.0-alpha" },
      { id: "d".repeat(32), filename: "<img src=x onerror=alert(1)>.aqbackup", size: 4, created: "invalid", version: "<svg>" },
      { id: "../../private", filename: "secret.aqbackup", size: 4, created: "invalid", version: "x" }
    ] };
    await page.locator("#backup-export-result:not([hidden])").waitFor();
    assert.equal(await page.locator("#backup-download").getAttribute("href"), "/bundle-backup/download/" + "a".repeat(32) + "?token=" + token());
    const downloadWait = page.waitForEvent("download");
    await page.locator("#backup-download").click();
    assert.equal((await downloadWait).suggestedFilename(), "fixture.aqbackup");
    await page.locator("#backup-tab-list").click();
    assert.equal(await page.locator("#backup-list li").count(), 2);
    assert.ok(!(await page.locator("#backup-list li").first().textContent()).includes("日期未知"), "Host epoch timestamps must display as dates");
    assert.equal(await page.locator("#backup-list img").count(), 0);
    assert.equal(await page.locator("#backup-list a[href^='https:']").count(), 0);
    revision++;
    await page.waitForFunction(expected => document.querySelector("#backup-list a")?.href.endsWith("token=" + expected), token());
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0, "No password or UI state is persisted in browser storage");
    await page.locator("#backup-tab-import").click();
    await page.locator("#backup-file").setInputFiles({ name: "wrong.zip", mimeType: "application/zip", buffer: Buffer.from("invalid fixture") });
    await page.locator("#backup-import-password").fill("FixturePassword-not-real");
    assert.equal(await page.locator("#backup-inspect").isDisabled(), true);
    await page.locator("#backup-file").setInputFiles({ name: "fixture.aqbackup", mimeType: "application/octet-stream", buffer: Buffer.from("encrypted fixture, no real data") });
    await page.locator("#backup-import-password").fill("short");
    assert.equal(await page.locator("#backup-inspect").isDisabled(), true);
    await page.locator("#backup-import-password").fill("FixturePassword-not-real");
    await page.locator("#backup-inspect:not([disabled])").click();
    assert.equal(await page.locator("#backup-import-password").inputValue(), "");
    await page.locator("#backup-plan:not([hidden])").waitFor();
    assert.equal(mutations.filter(item => item.pathname.endsWith("/restore")).length, 0, "Inspection cannot start restore");
    assert.equal(await page.locator("#backup-plan-version").textContent(), "0.7.0-alpha");
    assert.equal(await page.locator("#backup-plan-size").textContent(), "128 个文件 · 8.0 MiB");
    assert.equal(await page.locator("#backup-components dd").count(), 3);
    assert.equal(await page.locator("#backup-warnings img").count(), 0);
    assert.ok(!(await page.locator("#backup-warnings").textContent()).includes("PrivatePerson"));
    assert.equal(await page.locator("#backup-restore").isDisabled(), true);
    await page.locator("#backup-source-stopped").check();
    assert.equal(await page.locator("#backup-restore").isDisabled(), true, "Existing data requires a separate replacement confirmation");
    await page.locator("#backup-overwrite").check();
    assert.equal(await page.locator("#backup-restore").isDisabled(), false);
    if (process.env.BUNDLE_SCREENSHOTS) {
      fs.mkdirSync(process.env.BUNDLE_SCREENSHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "backup-restore-desktop-test.png") });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const dialog = await page.locator("#backup-dialog").boundingBox();
    assert.ok(dialog.x >= 0 && dialog.x + dialog.width <= 390);
    assert.ok(dialog.y >= 0 && dialog.y + dialog.height <= 844);
    assert.equal(await page.locator("#backup-dialog").evaluate(element => element.scrollWidth <= element.clientWidth), true);
    if (process.env.BUNDLE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "backup-restore-mobile-test.png") });
    abortRestore = true;
    await page.locator("#backup-restore").click();
    await page.locator("#backup-message").filter({ hasText: "重新连接" }).waitFor();
    assert.equal(mutations.filter(item => item.pathname.endsWith("/restore")).length, 1);
    await page.locator("#backup-close").click();
    state = { ...state, phase: "completed", active: false, plan: null, result: { kind: "restore" } };
    await page.waitForFunction(() => document.querySelector("#backup-label").textContent === "备份与恢复", null, { timeout: 12000 });
    assert.equal(mutations.filter(item => item.pathname.endsWith("/restore")).length, 1, "No POST retry across gateway disconnect");
    assert.equal(await native("astrbot").locator("#draft").inputValue(), "这是尚未保存的原生后台设置");
    assert.equal(await native("qwenpaw").locator("#draft").inputValue(), "记忆后台中的草稿");
    assert.equal(new URL(page.url()).hash, "#qwenpaw");
    await page.locator("#backup-toggle").click();
    await page.locator("#backup-reload:not([hidden])").waitFor();
    await page.locator("#backup-reload").click();
    await page.locator('#panel-qwenpaw:not([hidden]) iframe').waitFor();
    assert.equal(new URL(page.url()).hash, "#qwenpaw");
    await page.locator("#backup-toggle").click();
    await page.locator("#backup-tab-import").click();
    state = { ...state, phase: "ready_to_restore", active: false, result: null, plan: { ...plan, path_check: "failed" } };
    await page.locator("#backup-restore-result[hidden]").waitFor({ state: "attached" });
    assert.equal(await page.locator("#backup-plan").isVisible(), false, "Unvalidated paths must not offer restore");
    state = { ...state, phase: "failed", recovery_required: true, message: "raw E:\\PrivatePerson\\secret failure", error: "API_SECRET_SHOULD_NOT_DISPLAY" };
    await page.locator("#backup-message").filter({ hasText: "上次恢复尚未完成" }).waitFor();
    assert.ok(!(await page.locator("#backup-dialog").textContent()).includes("PrivatePerson"));
    assert.ok(!(await page.locator("#backup-dialog").textContent()).includes("API_SECRET"));
    assert.equal(await page.locator("#backup-inspect").isDisabled(), true);
    state = { ...state, phase: "failed", recovery_required: false, error: "authentication_failed" };
    await page.locator("#backup-message").filter({ hasText: "密码不正确" }).waitFor();
    state = { ...state, phase: "idle", recovery_required: false, plan: null };
    await page.locator("#backup-tab-export").click();
    await page.locator("#backup-generate:not([disabled])").waitFor();
    await page.locator("#backup-password").fill("close-clears-password");
    await page.locator("#backup-password-confirm").fill("close-clears-password");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#backup-password").inputValue(), "");
    assert.equal(await page.locator("#backup-password-confirm").inputValue(), "");
    assert.equal(await page.locator("#backup-file").inputValue(), "");
    const getsAfterClose = getCount;
    await page.waitForTimeout(2300);
    assert.equal(getCount, getsAfterClose, "An idle, closed backup dialog must stop polling");
    assert.equal(errors.length, 0, errors.join("\n"));
    assert.equal(mutations.length, 4, "Only explicit export, upload, inspect, and restore were sent");
    console.log("PASS: separate backup dialog; desktop/mobile; accessible tabs and focus; password validation/generation/clearing; no browser secret storage; export and safe current-token downloads; multipart upload; inspect without restore; explicit source-stop and overwrite gates; safe metadata; no POST retry on disconnect; active-only background polling; native drafts retained; recovery block; validated-path gate.");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
