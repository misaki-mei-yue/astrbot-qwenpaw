// Genuine gateway + browser; upstream pages are labelled local test substitutes.
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const child = spawn(process.env.BUNDLE_TEST_PYTHON || "python", [path.join(__dirname, "bundle_ui_fixture.py")], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  let browser;
  let fixtureError = "";
  child.stderr.on("data", data => { fixtureError = (fixtureError + data.toString()).slice(-2000); });
  try {
    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Local fixture did not start")), 20000);
      let text = "";
      child.stdout.on("data", data => { text += data.toString(); if (text.includes("\n")) { clearTimeout(timer); try { resolve(JSON.parse(text.split("\n")[0]).port); } catch (err) { reject(err); } } });
      child.once("error", reject);
      child.once("exit", code => { if (code) { clearTimeout(timer); reject(new Error("Fixture failed: " + fixtureError)); } });
    });
    browser = await chromium.launch({ headless: true, ...(process.env.BUNDLE_TEST_BROWSER ? { executablePath: process.env.BUNDLE_TEST_BROWSER } : {}), args: ["--no-proxy-server"] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    const updateToken = () => "fixture-update-token-" + tokenVersion;
    let tokenVersion = 1;
    let updateState = { enabled: false, current_version: "0.6.0-alpha", phase: "idle", active: false };
    const updateRequests = [];
    let restartFailures = 0;
    await page.route(`http://localhost:${port}/bundle-update**`, async route => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      if (request.method() === "GET") {
        if (restartFailures > 0) { restartFailures--; return route.abort("connectionrefused"); }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...updateState, csrf_token: updateToken() }) });
      }
      assert.equal(request.headers()["content-type"], "application/json");
      assert.equal(request.headers()["x-bundle-update-token"], updateToken());
      updateRequests.push({ pathname, body: request.postDataJSON() });
      if (pathname.endsWith("/check")) {
        assert.deepEqual(request.postDataJSON(), {});
        tokenVersion++;
        updateState = { ...updateState, phase: "available", active: false, candidate: {
          id: "b".repeat(32), version: "0.6.1-alpha", notes: "修复消息衔接。\n<img src=x onerror=alert(1)>", html_url: "https://github.com/example/project/releases/tag/v0.6.1-alpha",
          components: { astrbot: "4.25.1", qwenpaw: "2.2.1", napcat: "4.18.33" }
        } };
      } else if (pathname.endsWith("/apply")) {
        assert.deepEqual(request.postDataJSON(), { candidate_id: "b".repeat(32) });
        tokenVersion++;
        updateState = { ...updateState, phase: "updating", active: true, progress: 8, step: "正在备份个人数据" };
        restartFailures = 2;
      } else throw new Error("Unexpected update mutation: " + pathname);
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ request_id: "test-request" }) });
    });
    await page.goto(`http://localhost:${port}/`);
    await page.locator('#panel-astrbot:not([hidden]) iframe').waitFor();
    const frame = name => page.frameLocator(`#panel-${name} iframe`);
    await frame("astrbot").locator("#draft").fill("保留我正在编辑的配置");
    for (const name of ["qwenpaw", "napcat", "astrbot"]) {
      await page.locator(`[data-app="${name}"]`).click();
      await frame(name).locator("#call").click();
      await frame(name).locator("#result").filter({ hasText: name }).waitFor();
      const childFrame = page.frames().find(item => item.url().startsWith(`http://${name}.localhost:${port}/`));
      assert.equal(await childFrame.evaluate(() => localStorage.getItem("native-session")), name);
    }
    assert.equal(await frame("astrbot").locator("#draft").inputValue(), "保留我正在编辑的配置");
    assert.equal(new URL(page.url()).host, `localhost:${port}`);
    const qwenFrame = page.frames().find(item => item.url().includes("qwenpaw.localhost"));
    assert.equal(await qwenFrame.evaluate(() => document.featurePolicy.allowsFeature("microphone")), true);
    await page.locator('[data-app="qwenpaw"]').click();
    const downloadWait = page.waitForEvent("download");
    await frame("qwenpaw").getByText("下载测试文件", { exact: true }).click();
    assert.equal((await downloadWait).suggestedFilename(), "test.txt");
    const popupWait = page.waitForEvent("popup");
    await page.locator("#open-native").click();
    const popup = await popupWait;
    await popup.waitForLoadState();
    assert.equal(new URL(popup.url()).host, `qwenpaw.localhost:${port}`);
    await popup.close();
    // Read-only opening, explicit two-step confirmation, launcher-disabled help.
    assert.equal(updateRequests.length, 0);
    await page.locator("#update-toggle").click();
    await page.locator("#update-message").filter({ hasText: "新版启动器" }).waitFor();
    assert.equal(await page.locator("#update-check").isDisabled(), true);
    assert.equal(await page.locator("#update-apply").isVisible(), false);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#update-dialog").evaluate(el => el.open), false);
    assert.equal(await page.locator("#update-toggle").evaluate(el => el === document.activeElement), true);
    updateState = { ...updateState, enabled: true };
    await page.locator("#update-toggle").click();
    await page.locator("#update-check:not([disabled])").waitFor();
    assert.equal(updateRequests.length, 0, "Opening the dialog must not POST a check");
    await page.locator("#update-check").click();
    await page.locator("#update-target").filter({ hasText: "0.6.1-alpha" }).waitFor();
    assert.equal(await page.locator("#update-notes img").count(), 0);
    assert.ok((await page.locator("#update-notes").textContent()).includes("<img"));
    assert.equal(await page.locator("#update-astrbot").textContent(), "4.25.1");
    assert.equal(updateRequests.length, 1);
    assert.equal(await page.locator("#update-warning").isVisible(), true);
    if (process.env.BUNDLE_SCREENSHOTS) {
      fs.mkdirSync(process.env.BUNDLE_SCREENSHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "bundle-update-confirm-test.png") });
    }
    updateState = { ...updateState, update_capable: false, message: "本地源码有修改，仍可检查新版本，请先处理本地修改再更新。" };
    await page.locator("#update-message").filter({ hasText: "本地源码有修改" }).waitFor();
    assert.equal(await page.locator("#update-check").isDisabled(), false);
    assert.equal(await page.locator("#update-apply").isVisible(), false);
    assert.equal(updateRequests.length, 1);
    delete updateState.update_capable;
    delete updateState.message;
    await page.locator("#update-apply:not([hidden])").waitFor();
    await page.locator("#update-apply").click();
    await page.locator("#update-message").filter({ hasText: "重新连接" }).waitFor();
    assert.equal(updateRequests.length, 2);
    await page.locator("#update-close").click();
    // Closed dialogs continue polling only while an operation remains active.
    updateState = { ...updateState, phase: "completed", active: false, progress: 100, current_version: "0.6.1-alpha", message: "更新完成。" };
    await page.waitForFunction(() => document.querySelector("#update-current").textContent === "v0.6.1-alpha", null, { timeout: 12000 });
    assert.equal(updateRequests.length, 2, "A disconnect must not resubmit update operations");
    assert.equal(await frame("astrbot").locator("#draft").inputValue(), "保留我正在编辑的配置");
    assert.equal(new URL(page.url()).hash, "#qwenpaw");
    await page.locator("#update-toggle").click();
    await page.locator("#update-reload:not([hidden])").waitFor();
    await page.locator("#update-reload").click();
    await page.locator('#panel-qwenpaw:not([hidden]) iframe').waitFor();
    assert.equal(new URL(page.url()).hash, "#qwenpaw", "Refreshing after an update keeps the selected native application");
    await page.locator("#update-toggle").click();
    updateState = { ...updateState, phase: "failed", message: "恢复失败 E:\\PrivateUser\\backup；请查看启动器。", candidate: null };
    await page.locator("#update-error").filter({ hasText: "[本机路径]" }).waitFor();
    assert.ok(!(await page.locator("#update-dialog").textContent()).includes("PrivateUser"));
    assert.equal(await page.locator("#update-apply").isVisible(), false);
    updateState = { ...updateState, recovery_required: true, message: "恢复尚未完成，已暂停更新。" };
    await page.waitForFunction(() => document.querySelector("#update-check").disabled);
    assert.equal(await page.locator("#update-apply").isVisible(), false);
    updateState = { ...updateState, recovery_required: false, phase: "available", candidate: { id: "b".repeat(32), version: "0.6.1-alpha", components: { astrbot: "4.25.1", qwenpaw: "2.2.1", napcat: "4.18.33", shell: "arbitrary" } } };
    await page.waitForFunction(() => !document.querySelector("#update-check").disabled);
    assert.equal(await page.locator("#update-candidate").isVisible(), false, "Unknown component keys must not create an applicable candidate");
    assert.equal(await page.locator("#update-apply").isVisible(), false);
    updateState = { ...updateState, phase: "idle", candidate: null, message: "" };
    await page.keyboard.press("Escape");
    if (process.env.BUNDLE_SCREENSHOTS) {
      fs.mkdirSync(process.env.BUNDLE_SCREENSHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "bundle-desktop-test.png") });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('[data-app="napcat"]').click();
    await frame("napcat").locator("#call").click();
    await page.locator('[data-app="napcat"]').focus();
    await page.keyboard.press("Home");
    assert.equal(await page.locator('[data-app="astrbot"]').getAttribute("aria-selected"), "true");
    await page.locator("#update-toggle").click();
    await page.locator("#update-dialog[open]").waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const dialogBox = await page.locator("#update-dialog").boundingBox();
    assert.ok(dialogBox.x >= 0 && dialogBox.x + dialogBox.width <= 390);
    if (process.env.BUNDLE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "bundle-update-mobile-test.png") });
    await page.keyboard.press("Escape");
    if (process.env.BUNDLE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "bundle-mobile-test.png") });
    console.log("PASS: single entry; native origins; separate storage; POST; retained forms; iframe CSP; microphone policy; downloads; new window; mobile; keyboard navigation; update confirmation; disabled launcher; safe release notes; CSRF rotation; reconnect without repost; selected-page retention; private-path redaction.");
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
