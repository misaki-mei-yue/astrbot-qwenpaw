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
    if (process.env.BUNDLE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.BUNDLE_SCREENSHOTS, "bundle-mobile-test.png") });
    console.log("PASS: single entry; native origins; separate storage; POST; retained forms; iframe CSP; microphone policy; downloads; new window; mobile; keyboard navigation.");
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
