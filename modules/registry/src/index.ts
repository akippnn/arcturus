// src/index.ts - Registry module entry point

import { unlinkSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { StackRegistry } from "./registry.js";
import { createAPIServer } from "./api.js";
import { BusClient } from "./bus-client.js";

const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.()}`;
const SOCKET_PATH = process.env.REGISTRY_SOCKET || `${runtimeDir}/arcturus/registry.sock`;
const BUS_SOCKET = process.env.BUS_SOCKET || `${runtimeDir}/arcturus/bus.sock`;
const STACKS_DIR = process.env.STACKS_DIR || `${homedir()}/stacks`;
const ACTIVE_MANIFESTS_DIR = process.env.ACTIVE_MANIFESTS_DIR
  || `${homedir()}/.local/share/arcturus-deployer/active-manifests`;

async function main() {
  // Ensure socket directory exists
  try { mkdirSync(`${runtimeDir}/arcturus`, { recursive: true }); } catch { /* ignore */ }

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
