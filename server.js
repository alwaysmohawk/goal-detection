/*
 * HHOF goalie sim - dev receiver server
 * =====================================
 * Single-file Node app that:
 *   1. Serves a dev/test webpage at http://localhost:8080
 *   2. Runs a WebSocket broker at ws://localhost:8765
 *
 * The Python detector(s) connect as clients to the broker and send goal/no_goal
 * events. The webpage also connects as a client. Messages are routed by type:
 *   - From detectors: hello, heartbeat, goal, no_goal -> forwarded to all UI clients
 *   - From UI: shot_incoming, set_mode -> forwarded to all detector clients
 *
 * Run:    npm install ws && node server.js
 * Visit:  http://localhost:8080
 *
 * No build step, no framework. Just ws + http.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');

const HTTP_PORT = 8080;
const WS_PORT = 8765;

// ---- HTML page (inline; served from / and /index.html) ----
const HTML = fs.readFileSync(path.join(__dirname, 'receiver.html'), 'utf8');

const httpServer = http.createServer((req, res) => {
  if (req.url === '/' || req.url === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(HTML);
    return;
  }
  res.writeHead(404).end('not found');
});

httpServer.listen(HTTP_PORT, () => {
  console.log(`HTTP   http://localhost:${HTTP_PORT}`);
});

// ---- WebSocket broker ----
const wss = new WebSocketServer({ port: WS_PORT });
console.log(`WS     ws://localhost:${WS_PORT}`);

// Tag each connection with role: 'detector' | 'ui' | 'unknown' (until 'hello')
function broadcast(role, msg) {
  const raw = JSON.stringify(msg);
  for (const client of wss.clients) {
    if (client.readyState !== client.OPEN) continue;
    if (client._role === role) client.send(raw);
  }
}

wss.on('connection', (ws, req) => {
  ws._role = 'unknown';
  ws._id = Math.random().toString(36).slice(2, 8);
  console.log(`[${ws._id}] connected from ${req.socket.remoteAddress}`);

  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      console.warn(`[${ws._id}] bad JSON: ${data}`);
      return;
    }

    // Role inference: if it identifies with hello+net_id, it's a detector.
    // The web UI sends hello with role:'ui'.
    if (msg.type === 'hello') {
      if (msg.role === 'ui') ws._role = 'ui';
      else if (msg.net_id) {
        ws._role = 'detector';
        ws._netId = msg.net_id;
      }
      console.log(`[${ws._id}] hello -> role=${ws._role} net_id=${ws._netId || '-'}`);
      // Tell any UI clients about the new detector
      broadcast('ui', { type: 'detector_connected', net_id: ws._netId, ts: Date.now() / 1000 });
      return;
    }

    // Route based on sender role
    if (ws._role === 'detector') {
      // Forward all detector messages to UI clients
      broadcast('ui', msg);
    } else if (ws._role === 'ui') {
      // Forward UI commands to detectors
      broadcast('detector', msg);
    } else {
      console.warn(`[${ws._id}] message before hello: ${msg.type}`);
    }
  });

  ws.on('close', () => {
    console.log(`[${ws._id}] disconnected (role=${ws._role})`);
    if (ws._role === 'detector') {
      broadcast('ui', { type: 'detector_disconnected', net_id: ws._netId, ts: Date.now() / 1000 });
    }
  });
});
