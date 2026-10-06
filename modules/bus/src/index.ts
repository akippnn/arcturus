// src/index.ts - Message Bus entry point

import { MessageBus } from "./bus.js";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

function requiredPath(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} must be supplied by the canonical Arcturus layout`);
  }
  return value;
}

const SOCKET_PATH = requiredPath("BUS_SOCKET");

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
