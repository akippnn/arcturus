// src/index.ts - Registry module entry point

import { unlinkSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { StackRegistry } from "./registry.js";
import { createAPIServer } from "./api.js";
import { BusClient } from "./bus-client.js";

function requiredPath(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} must be supplied by the canonical Arcturus layout`);
  }
  return value;
}

const SOCKET_PATH = requiredPath("REGISTRY_SOCKET");
const BUS_SOCKET = requiredPath("BUS_SOCKET");
const STACKS_DIR = requiredPath("STACKS_DIR");
const ACTIVE_MANIFESTS_DIR = requiredPath("ACTIVE_MANIFESTS_DIR");

async function main() {
  // Ensure socket directory exists
  try { mkdirSync(dirname(SOCKET_PATH), { recursive: true }); } catch { /* ignore */ }

  // Clean up old socket
  try { unlinkSync(SOCKET_PATH); } catch { /* ignore */ }

  console.log(`Arcturus Registry starting...`);
  console.log(`Scanning stacks from: ${STACKS_DIR}`);
  console.log(`Scanning active releases from: ${ACTIVE_MANIFESTS_DIR}`);
  console.log(`Socket: ${SOCKET_PATH}`);

  const registry = new StackRegistry(
    STACKS_DIR,
    "*/arcturus.json",
    [ACTIVE_MANIFESTS_DIR],
  );

  // Connect to message bus
  const bus = new BusClient({ socketPath: BUS_SOCKET, clientName: "registry" });
  try {
    await bus.connect();
    console.log("Connected to message bus");
  } catch {
    console.warn("Message bus not available, operating standalone");
  }

  // Publish registry events to bus
  registry.onEvent((event) => {
    const topic = `stack.${event.type}`;
    bus.publish(topic, {
      stackName: event.stackName,
      stack: event.stack,
      timestamp: event.timestamp,
    });
  });

  // Initial scan
  await registry.scan();
  console.log(`Loaded ${registry.list().length} stacks`);

  // Start file watcher
  registry.watch();
  const scanTimer = setInterval(() => {
    registry.scan().catch((error) => console.error("Registry rescan failed:", error));
  }, 30000);

  // Start API server
  const server = createAPIServer(registry, SOCKET_PATH);
  server.listen(SOCKET_PATH, () => {
    console.log(`Registry API listening on ${SOCKET_PATH}`);
  });

  // Handle graceful shutdown
  process.on("SIGINT", () => {
    console.log("\nShutting down...");
    clearInterval(scanTimer);
    bus.disconnect();
    server.close(() => {
      try { unlinkSync(SOCKET_PATH); } catch { /* ignore */ }
      process.exit(0);
    });
  });

  process.on("SIGTERM", () => {
    clearInterval(scanTimer);
    bus.disconnect();
    server.close(() => {
      try { unlinkSync(SOCKET_PATH); } catch { /* ignore */ }
      process.exit(0);
    });
  });
}

main().catch(err => {
  console.error("Fatal error:", err);
  process.exit(1);
});
