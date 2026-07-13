// src/api.ts - HTTP API over Unix domain socket

import { createServer, type Server } from "node:http";
import { type StackRegistry, type RegistryEvent } from "./registry.js";

export function createAPIServer(registry: StackRegistry, socketPath: string): Server {
  const server = createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");

    try {
      const url = new URL(req.url || "/", "http://localhost");
      const method = req.method || "GET";
      const pathname = url.pathname;

      // CORS
      res.setHeader("Access-Control-Allow-Origin", "*");
      res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type");

      if (method === "OPTIONS") {
        res.writeHead(204);
        res.end();
        return;
      }

      // Health check
      if (pathname === "/health") {
        res.writeHead(200);
        res.end(JSON.stringify({ status: "ok", stacks: registry.list().length }));
        return;
      }

      // Host-local rescan used by the deployer after atomic manifest publication.
      if (pathname === "/rescan" && method === "POST") {
        await registry.scan();
        res.writeHead(200);
        res.end(JSON.stringify({ status: "rescanned", stacks: registry.list().length }));
        return;
      }

      // List all stacks
      if (pathname === "/stacks" && method === "GET") {
        const stacks = registry.list();
        const managed = url.searchParams.get("managed");
        let result = stacks;
        if (managed !== null) {
          const wantManaged = managed === "true";
          result = stacks.filter(s => s.spec.deploy?.managed === wantManaged);
        }
        res.writeHead(200);
        res.end(JSON.stringify(result));
        return;
      }

      // Get specific stack
      const stackMatch = pathname.match(/^\/stacks\/([^\/]+)$/);
      if (stackMatch && method === "GET") {
        const name = stackMatch[1];
        const stack = registry.get(name);
        if (!stack) {
          res.writeHead(404);
          res.end(JSON.stringify({ error: "Stack not found" }));
          return;
        }
        res.writeHead(200);
        res.end(JSON.stringify(stack));
        return;
      }

      // Get stack domains
      const domainMatch = pathname.match(/^\/stacks\/([^\/]+)\/domains$/);
      if (domainMatch && method === "GET") {
        const name = domainMatch[1];
        const stack = registry.get(name);
        if (!stack) {
          res.writeHead(404);
          res.end(JSON.stringify({ error: "Stack not found" }));
          return;
        }
        const domains: string[] = [];
        for (const service of Object.values(stack.spec.services)) {
          if (service.domains) domains.push(...service.domains);
        }
        res.writeHead(200);
        res.end(JSON.stringify({ stack: name, domains }));
        return;
      }

      // Resolve domain
      const resolveMatch = pathname.match(/^\/resolve\/(.+)$/);
      if (resolveMatch && method === "GET") {
        const domain = decodeURIComponent(resolveMatch[1]);
        const found = registry.findByDomain(domain);
        if (!found) {
          res.writeHead(404);
          res.end(JSON.stringify({ error: "Domain not found" }));
          return;
        }
        res.writeHead(200);
        res.end(JSON.stringify({
          domain,
          stack: found.stack.metadata.name,
          service: found.serviceName,
          port: found.service.port,
          internalHost: `${found.stack.metadata.name}-${found.serviceName}`,
        }));
        return;
      }

      // Event stream (SSE) for real-time updates
      if (pathname === "/events") {
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "Connection": "keep-alive",
        });

        const sendEvent = (event: RegistryEvent) => {
          res.write(`data: ${JSON.stringify(event)}\n\n`);
        };

        registry.onEvent(sendEvent);

        req.on("close", () => {
          // Listener will be cleaned up by registry's return function
        });
        return;
      }

      // Not found
      res.writeHead(404);
      res.end(JSON.stringify({ error: "Not found" }));
    } catch (err) {
      console.error("API error:", err);
      res.writeHead(500);
      res.end(JSON.stringify({ error: "Internal server error" }));
    }
  });

  return server;
}
