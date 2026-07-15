#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { access, mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve("_site");
const artifacts = resolve("tmp/browser/results");
const chromeCandidates = process.platform === "win32" ? [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
] : ["google-chrome", "chromium", "chromium-browser"];
async function findBrowser() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  for (const candidate of chromeCandidates) {
    try { await access(candidate); return candidate; } catch {}
  }
  throw new Error(`No supported browser found. Set CHROME_PATH; checked: ${chromeCandidates.join(", ")}`);
}
const chrome = await findBrowser();
const mime = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" };
const sleep = ms => new Promise(resolvePromise => setTimeout(resolvePromise, ms));

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    let file = resolve(root, `.${normalize(pathname)}`);
    assert(file === root || file.startsWith(root + "\\") || file.startsWith(root + "/"), "unsafe path");
    if ((await stat(file)).isDirectory()) file = join(file, "index.html");
    const body = await readFile(file);
    response.writeHead(200, { "content-type": mime[extname(file)] || "application/octet-stream" });
    response.end(body);
  } catch {
    response.writeHead(404); response.end("not found");
  }
});
await new Promise(resolvePromise => server.listen(0, "127.0.0.1", resolvePromise));
const siteUrl = `http://127.0.0.1:${server.address().port}`;
const profile = await mkdtemp(join(tmpdir(), "segfault-browser-"));
await mkdir(artifacts, { recursive: true });

const browser = spawn(chrome, [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
  "--disable-default-apps", "--disable-extensions", "--disable-sync", "--no-first-run",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
let browserErrors = "";
browser.stderr.on("data", chunk => { browserErrors += chunk; });

let socket;
try {
  const portFile = join(profile, "DevToolsActivePort");
  let debugPort;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { debugPort = Number((await readFile(portFile, "utf8")).split(/\r?\n/)[0]); break; } catch { await sleep(100); }
  }
  assert(debugPort, "Chrome DevTools port did not become available");
  console.log(`Browser started on DevTools port ${debugPort}.`);
  let targets;
  for (let attempt = 0; attempt < 50; attempt++) {
    try { targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json(); break; } catch { await sleep(100); }
  }
  const target = targets?.find(item => item.type === "page");
  assert(target?.webSocketDebuggerUrl, "Chrome page target unavailable");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolvePromise, reject) => { socket.addEventListener("open", resolvePromise, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  console.log("DevTools WebSocket connected.");

  let nextId = 0;
  const pending = new Map();
  const listeners = new Map();
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const handler = pending.get(message.id); pending.delete(message.id);
      if (message.error) handler?.reject(new Error(message.error.message)); else handler?.resolve(message.result);
    } else for (const handler of listeners.get(message.method) || []) handler(message.params);
  });
  const send = (method, params = {}) => new Promise((resolvePromise, reject) => {
    const id = ++nextId; pending.set(id, { resolve: resolvePromise, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const once = method => new Promise(resolvePromise => {
    const handler = params => { listeners.set(method, (listeners.get(method) || []).filter(item => item !== handler)); resolvePromise(params); };
    listeners.set(method, [...(listeners.get(method) || []), handler]);
  });
  const failures = [];
  listeners.set("Network.loadingFailed", [event => { if (event.type !== "Other" && !event.canceled) failures.push(`network: ${event.errorText}`); }]);
  listeners.set("Network.responseReceived", [event => { if (event.response.status >= 400 && event.response.url.startsWith(siteUrl)) failures.push(`${event.response.status}: ${event.response.url}`); }]);
  listeners.set("Runtime.exceptionThrown", [event => failures.push(`exception: ${event.exceptionDetails.text}`)]);
  listeners.set("Runtime.consoleAPICalled", [event => { if (event.type === "error") failures.push(`console: ${event.args.map(arg => arg.value || arg.description).join(" ")}`); }]);
  await Promise.all([send("Page.enable"), send("Runtime.enable"), send("Network.enable")]);
  console.log("DevTools domains enabled.");

  async function navigate(path, width, height) {
    console.log(`Navigating ${path} at ${width}x${height}.`);
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
    console.log("Metrics set.");
    await send("Page.navigate", { url: `${siteUrl}${path}` });
    console.log("Navigate command returned.");
    for (let attempt = 0; attempt < 200; attempt++) {
      const state = await send("Runtime.evaluate", { expression: "document.readyState", returnByValue: true });
      if (state.result.value === "complete") break;
      await sleep(50);
    }
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const images = await send("Runtime.evaluate", { expression: "[...document.images].every(i => i.complete && i.naturalWidth)", returnByValue: true });
      if (images.result.value) break;
      await send("Runtime.evaluate", { expression: "scrollTo(0, document.body.scrollHeight)" });
      await sleep(150);
    }
    await send("Runtime.evaluate", { expression: "scrollTo(0, 0)" });
  }
  async function evaluate(expression) {
    const result = await send("Runtime.evaluate", { expression: `JSON.stringify(${expression})`, returnByValue: true });
    return JSON.parse(result.result.value);
  }
  async function screenshot(name) {
    const metrics = await send("Page.getLayoutMetrics");
    const { width, height } = metrics.cssContentSize;
    const result = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width, height, scale: 1 } });
    await writeFile(join(artifacts, name), Buffer.from(result.data, "base64"));
  }

  await navigate("/", 1440, 1000);
  console.log("Desktop page loaded.");
  const desktop = await evaluate(`(() => ({
    title: document.title,
    width: document.documentElement.scrollWidth,
    featured: document.querySelectorAll('.featured-grid .project-card').length,
    archived: document.querySelectorAll('.archive-grid .project-card').length,
    featuredColumns: getComputedStyle(document.querySelector('.featured-grid')).gridTemplateColumns.split(' ').length,
    archiveColumns: getComputedStyle(document.querySelector('.archive-grid')).gridTemplateColumns.split(' ').length,
    brokenImages: [...document.images].filter(i => !i.complete || !i.naturalWidth).map(i => i.src)
  }))()`);
  assert(desktop.title.includes("Software Engineer"), `wrong desktop page: ${desktop.title}`);
  assert(desktop.width <= 1440, `desktop horizontal overflow: ${desktop.width}px`);
  assert(desktop.featured === 6 && desktop.archived === 18, "desktop project counts are wrong");
  assert(desktop.featuredColumns === 2 && desktop.archiveColumns === 3, "desktop grid columns are wrong");
  assert(desktop.brokenImages.length === 0, `broken desktop images: ${desktop.brokenImages.join(", ")}`);
  await screenshot("desktop-full.png");

  await navigate("/", 390, 844);
  const mobile = await evaluate(`(() => {
    const meme = document.querySelector('.meme-signoff img').getBoundingClientRect();
    return {
      width: document.documentElement.scrollWidth,
      navHidden: getComputedStyle(document.querySelector('.nav-links')).display === 'none',
      featuredColumns: getComputedStyle(document.querySelector('.featured-grid')).gridTemplateColumns.split(' ').length,
      archiveColumns: getComputedStyle(document.querySelector('.archive-grid')).gridTemplateColumns.split(' ').length,
      meme: [Math.round(meme.width), Math.round(meme.height)],
      brokenImages: [...document.images].filter(i => !i.complete || !i.naturalWidth).map(i => i.src)
    };
  })()`);
  assert(mobile.width <= 390, `mobile horizontal overflow: ${mobile.width}px`);
  assert(mobile.navHidden, "mobile navigation did not collapse");
  assert(mobile.featuredColumns === 1 && mobile.archiveColumns === 1, "mobile grids did not collapse");
  assert(mobile.meme[0] <= 56 && mobile.meme[1] <= 72, `meme too large: ${mobile.meme.join("x")}`);
  assert(mobile.brokenImages.length === 0, `broken mobile images: ${mobile.brokenImages.join(", ")}`);
  await screenshot("mobile-full.png");

  await navigate("/ghost.html", 1280, 900);
  const ghost = await evaluate(`(() => ({ title: document.title, h1: document.querySelector('h1')?.textContent.trim(), iframe: Boolean(document.querySelector('iframe[title]')), width: document.documentElement.scrollWidth }))()`);
  assert(ghost.title.includes("WASM Battle Arena") && ghost.h1 === "WASM Battle Arena", "ghost project page did not render");
  assert(ghost.iframe && ghost.width <= 1280, "ghost page iframe or layout failed");
  await screenshot("ghost-full.png");

  const localFailures = failures.filter(failure => failure.includes(siteUrl) || failure.startsWith("exception:") || failure.startsWith("console:"));
  assert(localFailures.length === 0, `browser failures:\n${localFailures.join("\n")}`);
  console.log("Browser validation passed: desktop, mobile, ghost page, images, grids, overflow, console/network.");
} finally {
  try { socket?.close(); } catch {}
  browser.kill();
  await new Promise(resolvePromise => browser.once("exit", resolvePromise));
  server.close();
  for (let attempt = 0; attempt < 20; attempt++) {
    try { await rm(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }); break; }
    catch (error) { if (attempt === 19) console.warn(`Could not remove temporary browser profile: ${error.message}`); await sleep(100); }
  }
  if (browserErrors && /FATAL|uncaught/i.test(browserErrors)) console.error(browserErrors);
}
