// src/index.ts - Message Bus entry point

import { MessageBus } from "./bus.js";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.()}`;
const SOCKET_PATH = process.env.BUS_SOCKET || `${runtimeDir}/arcturus/bus.sock`;

async function main() {
  try { mkdirSync(dirname(SOCKET_PATH), { recursive: true }); } catch { /* ignore */ }

  const bus = new MessageBus(SOCKET_PATH);
  await bus.start();

  console.log("Arcturus Message Bus running");
  console.log("Socket:", SOCKET_PATH);

  process.on("SIGINT", async () => {
    console.log("\nShutting down bus...");
    await bus.stop();
    process.exit(0);
  });

  process.on("SIGTERM", async () => {
    await bus.stop();
    process.exit(0);
  });
}

main().catch(err => {
  console.error("Fatal error:", err);
  process.exit(1);
});
