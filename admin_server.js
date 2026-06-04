/**
 * HHOF Goal Detector — Admin Server
 *
 * HTTP on port 8090: serves admin.html and REST API
 * WS  on port 8766: accepts the goal detector's admin connection
 * WS  on port 8090: browser clients connect here for live status
 *
 * Runs as an NSSM service (SYSTEM account), so it can restart the
 * goal detector service without elevation.
 *
 * Environment variables set by install.ps1 / NSSM:
 *   GOAL_DETECTOR_SERVICE_NAME  - NSSM service name to restart (default: hhof-goal-detector)
 *   UV_PATH                     - path to uv.exe (optional, falls back to PATH)
 */

"use strict";

const http    = require("http");
const fs      = require("fs");
const path    = require("path");
const { execFile, execFileSync, spawnSync } = require("child_process");
const { spawn } = require("child_process");
const WebSocket = require("ws");

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const REPO_ROOT      = __dirname;
const CONFIG_PATH    = path.join(REPO_ROOT, "config.json");
const ADMIN_HTML     = path.join(REPO_ROOT, "admin.html");
const HTTP_PORT      = 8090;
const DETECTOR_WS_PORT = 8766;
const SERVICE_NAME      = process.env.GOAL_DETECTOR_SERVICE_NAME || "hhof-goal-detector";
const CALIBRATE_TASK    = process.env.CALIBRATE_TASK_NAME || null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function findUv() {
    const candidates = [
        process.env.UV_PATH,
        path.join(process.env.USERPROFILE || "", ".local", "bin", "uv.exe"),
    ].filter(Boolean);

    for (const p of candidates) {
        if (fs.existsSync(p)) return p;
    }
    // Fall back to PATH
    const r = spawnSync("uv", ["--version"], { shell: false });
    if (r.status === 0) return "uv";
    return null;
}

function findNssm() {
    const fromEnv = process.env.NSSM_PATH;
    if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
    const r = spawnSync("nssm", ["version"], { shell: false });
    if (r.status === 0) return "nssm";
    return null;
}

function readConfig() {
    if (!fs.existsSync(CONFIG_PATH)) return {};
    try { return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")); }
    catch { return {}; }
}

function writeConfig(data) {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2));
}

function jsonResponse(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) });
    res.end(payload);
}

function readBody(req) {
    return new Promise((resolve, reject) => {
        let body = "";
        req.on("data", d => { body += d; if (body.length > 1e6) req.destroy(); });
        req.on("end", () => resolve(body));
        req.on("error", reject);
    });
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let detectorSocket  = null;   // single WSClient connection from goal_detector.py
let browserClients  = new Set();
let lastHeartbeat   = null;   // most recent heartbeat msg for new browser connections
let calibrating     = false;  // true while calibration subprocess is running

function broadcast(msg) {
    const raw = typeof msg === "string" ? msg : JSON.stringify(msg);
    for (const c of browserClients) {
        if (c.readyState === WebSocket.OPEN) c.send(raw);
    }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

const httpServer = http.createServer(async (req, res) => {
    const url = req.url.split("?")[0];

    // --- Serve admin panel ---
    if (req.method === "GET" && (url === "/" || url === "/admin.html")) {
        if (!fs.existsSync(ADMIN_HTML)) {
            res.writeHead(404); res.end("admin.html not found");
            return;
        }
        const html = fs.readFileSync(ADMIN_HTML);
        res.writeHead(200, { "Content-Type": "text/html" });
        res.end(html);
        return;
    }

    // --- GET /api/config ---
    if (req.method === "GET" && url === "/api/config") {
        jsonResponse(res, 200, readConfig());
        return;
    }

    // --- POST /api/config ---
    if (req.method === "POST" && url === "/api/config") {
        try {
            const body = await readBody(req);
            const data = JSON.parse(body);
            writeConfig(data);
            jsonResponse(res, 200, { ok: true });
        } catch (e) {
            jsonResponse(res, 400, { error: String(e) });
        }
        return;
    }

    // --- POST /api/restart ---
    if (req.method === "POST" && url === "/api/restart") {
        const nssm = findNssm();
        if (!nssm) { jsonResponse(res, 500, { error: "nssm not found" }); return; }

        // Respond immediately; restart happens async
        jsonResponse(res, 200, { ok: true });
        broadcast({ type: "admin", event: "restarting" });

        execFile(nssm, ["restart", SERVICE_NAME], (err) => {
            if (err) {
                console.error("nssm restart failed:", err.message);
                broadcast({ type: "admin", event: "restart_failed", error: err.message });
            } else {
                broadcast({ type: "admin", event: "restarted" });
            }
        });
        return;
    }

    // --- POST /api/calibrate ---
    if (req.method === "POST" && url === "/api/calibrate") {
        if (calibrating) {
            jsonResponse(res, 409, { error: "calibration already running" });
            return;
        }
        const nssm = findNssm();
        if (!nssm) { jsonResponse(res, 500, { error: "nssm not found" }); return; }
        if (!CALIBRATE_TASK) { jsonResponse(res, 500, { error: "CALIBRATE_TASK_NAME not configured — re-run install.ps1" }); return; }

        jsonResponse(res, 200, { ok: true });
        calibrating = true;
        broadcast({ type: "admin", event: "calibrating" });

        // Stop detector service so it releases the camera
        try { spawnSync(nssm, ["stop", SERVICE_NAME], { shell: false }); } catch {}

        // Trigger the calibration scheduled task, which runs as the interactive user
        // so OpenCV windows appear on the local display (SYSTEM services run in
        // Session 0 and cannot open GUI windows in the user's session).
        const runResult = spawnSync("schtasks", ["/run", "/tn", CALIBRATE_TASK], { shell: false });
        if (runResult.status !== 0) {
            calibrating = false;
            broadcast({ type: "admin", event: "calibration_failed", code: runResult.status,
                        error: "schtasks /run failed — task may not exist, re-run install.ps1" });
            try { spawnSync(nssm, ["start", SERVICE_NAME], { shell: false }); } catch {}
            return;
        }

        // Poll every 2s: wait until we first see Running, then wait for it to stop.
        let sawRunning = false;
        let polls = 0;
        const MAX_POLLS = 300; // 10 minutes
        const timer = setInterval(() => {
            polls++;
            const q = spawnSync("schtasks", ["/query", "/tn", CALIBRATE_TASK, "/fo", "CSV", "/nh"], { shell: false });
            const out = q.stdout ? q.stdout.toString() : "";
            if (out.includes('"Running"')) {
                sawRunning = true;
            } else if (sawRunning || polls >= MAX_POLLS) {
                clearInterval(timer);
                calibrating = false;
                broadcast({ type: "admin", event: sawRunning ? "calibrated" : "calibration_failed",
                            code: sawRunning ? 0 : 1 });
                try { spawnSync(nssm, ["start", SERVICE_NAME], { shell: false }); } catch {}
            }
        }, 2000);
        return;
    }

    // --- POST /api/set-mode ---
    if (req.method === "POST" && url === "/api/set-mode") {
        try {
            const body = await readBody(req);
            const { mode } = JSON.parse(body);
            if (mode !== "always_on" && mode !== "armed") {
                jsonResponse(res, 400, { error: "mode must be 'always_on' or 'armed'" }); return;
            }
            if (!detectorSocket || detectorSocket.readyState !== WebSocket.OPEN) {
                jsonResponse(res, 503, { error: "detector not connected" }); return;
            }
            detectorSocket.send(JSON.stringify({ type: "set_mode", mode }));
            jsonResponse(res, 200, { ok: true });
        } catch (e) {
            jsonResponse(res, 400, { error: String(e) });
        }
        return;
    }

    // --- POST /api/status ---
    if (req.method === "GET" && url === "/api/status") {
        jsonResponse(res, 200, {
            detector_connected: detectorSocket !== null && detectorSocket.readyState === WebSocket.OPEN,
            calibrating,
            last_heartbeat: lastHeartbeat,
        });
        return;
    }

    res.writeHead(404); res.end("not found");
});

// ---------------------------------------------------------------------------
// Browser WebSocket (same port as HTTP)
// ---------------------------------------------------------------------------

const browserWss = new WebSocket.Server({ server: httpServer });

browserWss.on("connection", (ws) => {
    browserClients.add(ws);

    // Send current status immediately so the panel doesn't wait a full second
    ws.send(JSON.stringify({
        type: "admin",
        event: "connected",
        detector_connected: detectorSocket !== null && detectorSocket.readyState === WebSocket.OPEN,
        calibrating,
    }));
    if (lastHeartbeat) {
        ws.send(JSON.stringify(lastHeartbeat));
    }

    ws.on("close", () => browserClients.delete(ws));
    ws.on("error", () => browserClients.delete(ws));
});

// ---------------------------------------------------------------------------
// Detector WebSocket (port 8766)
// ---------------------------------------------------------------------------

const detectorWss = new WebSocket.Server({ port: DETECTOR_WS_PORT });

detectorWss.on("connection", (ws) => {
    if (detectorSocket) {
        // Replace stale connection
        try { detectorSocket.terminate(); } catch {}
    }
    detectorSocket = ws;
    console.log("Detector connected");
    broadcast({ type: "admin", event: "detector_connected" });

    ws.on("message", (data) => {
        let msg;
        try { msg = JSON.parse(data.toString()); } catch { return; }
        if (msg.type === "heartbeat") lastHeartbeat = msg;
        broadcast(msg);
    });

    ws.on("close", () => {
        detectorSocket = null;
        lastHeartbeat = null;
        console.log("Detector disconnected");
        broadcast({ type: "admin", event: "detector_disconnected" });
    });

    ws.on("error", () => {});
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

httpServer.listen(HTTP_PORT, () => {
    console.log(`Admin panel:  http://localhost:${HTTP_PORT}`);
    console.log(`Detector WS:  ws://localhost:${DETECTOR_WS_PORT}`);
    console.log(`Service name: ${SERVICE_NAME}`);
});
