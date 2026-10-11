import { Browser, Page } from 'rebrowser-puppeteer-core';
import { createCursor, GhostCursor } from 'ghost-cursor';
import kill from 'tree-kill';
import { spawn } from 'child_process';
import * as net from 'net';
import * as fs from 'fs';

// ── X display for the headed Chrome ──────────────────────────────────────────────────────────────────────────────
// The "xvfb" npm package waits only ~0.5 s for Xvfb to come up and then throws "Could not start Xvfb." On a busy or
// slow server that is not enough; Chrome then starts without a display, exits at once and we only see
// "connect ECONNREFUSED 127.0.0.1:<port>". So: reuse a display that already works, otherwise start Xvfb ourselves and
// wait up to 15 s for its socket.
function xSocketAlive(display: string): Promise<boolean> {
    const m = /^:(\d+)/.exec(display || "");
    if (!m) return Promise.resolve(false);
    return new Promise(resolve => {
        const s = net.createConnection({ path: `/tmp/.X11-unix/X${m[1]}` });
        const done = (ok: boolean) => { try { s.destroy(); } catch { } resolve(ok); };
        s.setTimeout(1000, () => done(false));
        s.once('connect', () => done(true));
        s.once('error', () => done(false));
    });
}

async function ensureDisplay(): Promise<{ stopSync: () => void } | null> {
    for (const d of [process.env.DISPLAY || "", ":99"]) {
        if (d && await xSocketAlive(d)) {
            process.env.DISPLAY = d;
            console.log(`[cf-bypass] using the existing X display ${d}`);
            return { stopSync: () => { } };
        }
    }
    for (let n = 90; n < 130; n++) {
        const display = `:${n}`;
        const lock = `/tmp/.X${n}-lock`;
        if (fs.existsSync(lock)) {
            if (await xSocketAlive(display)) continue;          // someone else's live display
            try { fs.unlinkSync(lock); } catch { continue; }    // stale lock from a crashed Xvfb
        }
        let exited = false;
        let proc: any;
        try {
            proc = spawn("Xvfb", [display, "-screen", "0", "1920x1080x24", "-ac", "-nolisten", "tcp"], { stdio: "ignore" });
        } catch (err: any) {
            console.error(`[cf-bypass] cannot run Xvfb: ${err?.message || err}`);
            return null;
        }
        proc.once("exit", () => { exited = true; });
        proc.once("error", (e: any) => { exited = true; console.error(`[cf-bypass] Xvfb error: ${e?.message || e}`); });
        for (let i = 0; i < 150 && !exited; i++) {              // up to 15 s
            if (await xSocketAlive(display)) {
                process.env.DISPLAY = display;
                console.log(`[cf-bypass] started Xvfb on ${display}`);
                return { stopSync: () => { try { proc.kill("SIGKILL"); } catch { } } };
            }
            await new Promise(r => setTimeout(r, 100));
        }
        try { proc.kill("SIGKILL"); } catch { }
        if (!exited) {
            console.error(`[cf-bypass] Xvfb on ${display} did not become ready within 15 s`);
            return null;
        }
        // it exited straight away (display number race) -> try the next number
    }
    return null;
}

async function checkTurnstile({ page }: { page: Page }): Promise<boolean> {
    return new Promise(async (resolve, reject) => {
        var waitInterval = setTimeout(() => { clearTimeout(waitInterval); resolve(false) }, 5000);
        try {
            const elements = await page.$$('[name="cf-turnstile-response"]');
            if (elements.length <= 0) {

                const coordinates = await page.evaluate(() => {
                    let coordinates: any = [];
                    document.querySelectorAll('div').forEach(item => {
                        try {
                            let itemCoordinates = item.getBoundingClientRect()
                            let itemCss = window.getComputedStyle(item)
                            if (itemCss.margin == "0px" && itemCss.padding == "0px" && itemCoordinates.width > 290 && itemCoordinates.width <= 310 && !item.querySelector('*')) {
                                coordinates.push({ x: itemCoordinates.x, y: item.getBoundingClientRect().y, w: item.getBoundingClientRect().width, h: item.getBoundingClientRect().height })
                            }
                        } catch (err) { }
                    });

                    if (coordinates.length <= 0) {
                        document.querySelectorAll('div').forEach(item => {
                            try {
                                let itemCoordinates = item.getBoundingClientRect()
                                if (itemCoordinates.width > 290 && itemCoordinates.width <= 310 && !item.querySelector('*')) {
                                    coordinates.push({ x: itemCoordinates.x, y: item.getBoundingClientRect().y, w: item.getBoundingClientRect().width, h: item.getBoundingClientRect().height })
                                }
                            } catch (err) { }
                        });

                    }

                    return coordinates
                })

                for (const item of coordinates) {
                    try {
                        let x = item.x + 30;
                        let y = item.y + item.h / 2;
                        await page.mouse.click(x, y);
                    } catch (err) { }
                }
                return resolve(true)
            }

            for (const element of elements) {
                try {
                    const parentElement = await element.evaluateHandle((el: any) => el.parentElement);
                    const box = await (parentElement as any).boundingBox();
                    if (box) {
                        let x = box.x + 30;
                        let y = box.y + box.height / 2;
                        await page.mouse.click(x, y);
                    }
                } catch (err) { }
            }
            clearTimeout(waitInterval)
            resolve(true)
        } catch (err) {
            clearTimeout(waitInterval)
            resolve(false)
        }
    })
}

interface PageControllerOptions {
    browser: Browser;
    page: Page & { realCursor?: GhostCursor; realClick?: GhostCursor["click"] };
    proxy: any;
    turnstile: boolean;
    xvfbsession: any;
    pid: number;
    plugins: any[];
    killProcess?: boolean;
    chrome?: any;
}

async function pageController({ browser, page, proxy, turnstile, xvfbsession, pid, plugins, killProcess = false, chrome }: PageControllerOptions) {

    let solveStatus = turnstile

    page.on('close', () => {
        solveStatus = false
    });


    browser.on('disconnected', async () => {
        solveStatus = false
        if (killProcess === true) {
            if (xvfbsession) try { xvfbsession.stopSync() } catch (err) { }
            if (chrome) try { chrome.kill() } catch (err) { console.log(err); }
            if (pid) try { kill(pid, 'SIGKILL', () => { }) } catch (err) { }
        }
    });

    async function turnstileSolver() {
        while (solveStatus) {
            await checkTurnstile({ page }).catch(() => { });
            await new Promise(r => setTimeout(r, 1000));
        }
        return
    }

    turnstileSolver()

    if (proxy.username && proxy.password) await page.authenticate({ username: proxy.username, password: proxy.password });

    if (plugins.length > 0) {
        for (const plugin of plugins) {
            plugin.onPageCreated(page)
        }
    }

    const cursor = createCursor(page);
    page.realCursor = cursor
    page.realClick = cursor.click

    return page
}

export interface PageWithCursor extends Page {
    realClick: GhostCursor["click"];
    realCursor: GhostCursor;
}

export interface ProxyOptions {
    host: string;
    port: number;
    username?: string;
    password?: string;
}

export interface Options {
    args?: string[];
    headless?: boolean | "auto";
    customConfig?: import("chrome-launcher").Options;
    proxy?: ProxyOptions;
    turnstile?: boolean;
    connectOption?: import("rebrowser-puppeteer-core").ConnectOptions;
    disableXvfb?: boolean;
    plugins?: import("puppeteer-extra").PuppeteerExtraPlugin[];
    ignoreAllFlags?: boolean;
}

export interface ConnectResult {
    browser: Browser;
    page: PageWithCursor;
}

export async function connect({
    args = [],
    headless = false,
    customConfig = {},
    proxy = {} as ProxyOptions,
    turnstile = false,
    connectOption = {},
    disableXvfb = false,
    plugins = [],
    ignoreAllFlags = false,
}: Options = {}): Promise<ConnectResult> {
    const { launch, Launcher } = await import("chrome-launcher");

    let Xvfb: any;
    try {
        Xvfb = require("xvfb");
    } catch {

    }

    let puppeteer = require("rebrowser-puppeteer-core");

    let xvfbsession: any = null;
    if (headless == "auto") headless = false;

    if (process.platform === "linux" && disableXvfb === false) {
        xvfbsession = await ensureDisplay();
    }
    if (process.platform === "linux" && disableXvfb === false && !xvfbsession) {
        try {
            xvfbsession = new Xvfb({
                silent: true,
                xvfb_args: ["-screen", "0", "1920x1080x24", "-ac"],
            });
            xvfbsession.startSync();
        } catch (err: any) {
            console.log(
                "You are running on a Linux platform but do not have xvfb installed. The browser can be captured. Please install it with the following command\n\nsudo apt-get install xvfb\n\n" +
                err.message
            );
        }
    }

    let chromeFlags: string[];
    if (ignoreAllFlags === true) {
        chromeFlags = [
            ...args,
            ...(headless !== false ? [`--headless=${headless}`] : []),
            ...(proxy && proxy.host && proxy.port
                ? [`--proxy-server=${proxy.host}:${proxy.port}`]
                : []),
        ];
    } else {

        const flags = Launcher.defaultFlags();

        const indexDisableFeatures = flags.findIndex((flag) => flag.startsWith('--disable-features'));
        if (indexDisableFeatures !== -1) {
            flags[indexDisableFeatures] = `${flags[indexDisableFeatures]},AutomationControlled`;
        }

        const indexComponentUpdateFlag = flags.findIndex((flag) => flag.startsWith('--disable-component-update'));
        if (indexComponentUpdateFlag !== -1) {
            flags.splice(indexComponentUpdateFlag, 1);
        }
        chromeFlags = [
            ...flags,
            ...args,
            ...(headless !== false ? [`--headless=${headless}`] : []),
            ...(proxy && proxy.host && proxy.port
                ? [`--proxy-server=${proxy.host}:${proxy.port}`]
                : []),
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ];
    }
    // Chrome sometimes dies right after it starts (low RAM, a busy Xvfb display). That used to surface only as
    // "connect ECONNREFUSED 127.0.0.1:<random port>" - retry once and log what we know.
    let chrome: any;
    let browser: any;
    for (let attempt = 1; ; attempt++) {
        try {
            chrome = await launch({
                ignoreDefaultFlags: true,
                chromeFlags,
                ...customConfig,
            });

            if (plugins && plugins.length > 0) {
                const { addExtra } = await import("puppeteer-extra");
                puppeteer = addExtra(puppeteer);
                for (const item of plugins) {
                    puppeteer.use(item);
                }
            }

            browser = await puppeteer.connect({
                browserURL: `http://127.0.0.1:${chrome.port}`,
                ...connectOption,
            });
            break;
        } catch (err: any) {
            console.error(
                `[cf-bypass] Chrome start attempt ${attempt} failed: ${err?.message || err} ` +
                `(CHROME_PATH=${process.env.CHROME_PATH || "-"} DISPLAY=${process.env.DISPLAY || "-"})`
            );
            if (chrome) { try { chrome.kill(); } catch { } chrome = null; }
            if (attempt >= 2) {
                if (xvfbsession) { try { xvfbsession.stopSync(); } catch { } }
                throw err;
            }
            await new Promise(r => setTimeout(r, 2000));
        }
    }

    let [page] = await browser.pages();

    let pageControllerConfig = {
        browser,
        page,
        proxy,
        turnstile,
        xvfbsession,
        pid: chrome.pid,
        plugins: plugins || [],
    };

    page = await pageController({
        ...pageControllerConfig,
        killProcess: true,
        chrome,
    });

    browser.on("targetcreated", async (target: any) => {
        if (target.type() === "page") {
            let newPage = await target.page();
            pageControllerConfig.page = newPage;
            newPage = await pageController(pageControllerConfig);
        }
    });

    return {
        browser,
        page,
    };
}
