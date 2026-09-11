import http from "node:http";
import https from "node:https";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const port = Number(process.env.PORT || 8787);
const root = dirname(fileURLToPath(import.meta.url));

const server = http.createServer(async (request, response) => {
  try {
    if (request.method === "GET" && (request.url === "/" || request.url === "/index.html")) {
      const html = await readFile(join(root, "index.html"), "utf8");
      send(response, 200, html, "text/html; charset=utf-8");
      return;
    }

    if (request.method === "POST" && request.url === "/proxy-trigger") {
      const body = await readJsonBody(request);
      const result = await callTriggerEndpoint(body);
      send(response, result.statusCode, result.body, result.contentType);
      return;
    }

    send(response, 404, JSON.stringify({ error: "not_found" }), "application/json");
  } catch (error) {
    send(
      response,
      500,
      JSON.stringify({ error: "proxy_error", message: String(error.message || error) }),
      "application/json"
    );
  }
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Runtime trigger test page: http://127.0.0.1:${port}`);
});

function send(response, statusCode, body, contentType) {
  response.writeHead(statusCode, {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function readJsonBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

function callTriggerEndpoint(body) {
  return new Promise((resolve, reject) => {
    const endpoint = String(body.endpoint || "").trim();
    if (!endpoint) {
      reject(new Error("endpoint is required"));
      return;
    }

    const target = new URL(endpoint);
    const payload = JSON.stringify(body.payload || {});
    const token = String(body.token || "").trim();
    const headers = {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(payload),
    };
    if (token) {
      headers.Authorization = token.toLowerCase().startsWith("bearer ")
        ? token
        : `Bearer ${token}`;
    }

    const client = target.protocol === "https:" ? https : http;
    const upstream = client.request(
      {
        method: "PUT",
        hostname: target.hostname,
        port: target.port || (target.protocol === "https:" ? 443 : 80),
        path: `${target.pathname}${target.search}`,
        headers,
        rejectUnauthorized: false,
      },
      (upstreamResponse) => {
        const chunks = [];
        upstreamResponse.on("data", (chunk) => chunks.push(chunk));
        upstreamResponse.on("end", () => {
          resolve({
            statusCode: upstreamResponse.statusCode || 502,
            contentType: upstreamResponse.headers["content-type"] || "application/json",
            body: Buffer.concat(chunks),
          });
        });
      }
    );

    upstream.setTimeout(30000, () => upstream.destroy(new Error("upstream timeout")));
    upstream.on("error", reject);
    upstream.write(payload);
    upstream.end();
  });
}
