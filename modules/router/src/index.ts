// src/index.ts - Router module entry point

import { Router } from "./router.js";
import { NetworkManager } from "./network.js";
import { BusClient } from "./bus-client.js";
import { homedir } from "node:os";

const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.()}`;

const CONFIG = {
  vhostsDir: process.env.VHOSTS_DIR || `${homedir()}/stacks/portal/config/vhosts.d`,
  nginxContainer: process.env.NGINX_CONTAINER || "portal-nginx",
  baseDomain: process.env.BASE_DOMAIN || "example.org",
  certDomain: process.env.CERT_DOMAIN || "example.org",
  apexService: process.env.ARCTURUS_APEX_SERVICE || undefined,
  registrySocket: process.env.REGISTRY_SOCKET || `${runtimeDir}/arcturus/registry.sock`,
  busSocket: process.env.BUS_SOCKET || `${runtimeDir}/arcturus/bus.sock`,
  statusFile: process.env.ROUTER_STATUS_FILE || `${runtimeDir}/arcturus/router-status.json`,
  containerCli: (process.env.CONTAINER_CLI || "podman") as "podman" | "podman-remote" | "docker",
};

async function main() {
  console.log("Arcturus Router starting...");
  console.log("Vhosts dir:", CONFIG.vhostsDir);
  console.log("Base domain:", CONFIG.baseDomain);

  const router = new Router(CONFIG);
  const networkMgr = new NetworkManager(CONFIG.containerCli, CONFIG.nginxContainer);

  // Connect to message bus
  const bus = new BusClient({ socketPath: CONFIG.busSocket, clientName: "router" });
  try {
    await bus.connect();
    console.log("Connected to message bus");

    // Listen for stack events from Registry
    bus.on("stack.discovered", async (payload) => {
      console.log("[Bus] Stack discovered:", payload.stackName);
      await applyNetwork(payload.stack, networkMgr);
      await fullReapply(router, networkMgr);
    });

    bus.on("stack.updated", async (payload) => {
      console.log("[Bus] Stack updated:", payload.stackName);
      await applyNetwork(payload.stack, networkMgr);
      await fullReapply(router, networkMgr);
    });

    bus.on("stack.removed", async (payload) => {
      console.log("[Bus] Stack removed:", payload.stackName);
      await router.removeVhost(payload.stackName);
      await networkMgr.removeStackNetwork(payload.stackName);
      await fullReapply(router, networkMgr);
    });
  } catch {
    console.warn("Message bus not available, falling back to polling");
  }

  // Initial sync
  try {
    await fullReapply(router, networkMgr);
    console.log("Nginx configs generated");
  } catch (err) {
    console.error("Initial sync failed:", (err as Error).message);
  }

  // Fallback polling if bus unavailable
  setInterval(async () => {
    try {
      await fullReapply(router, networkMgr);
    } catch (err) {
      console.error("Sync failed:", (err as Error).message);
    }
  }, 30000);
  console.log("Router polling every 30s");
}

async function fullReapply(router: Router, networkMgr: NetworkManager): Promise<void> {
  const stacks = await router.fetchStacks();
  for (const stack of stacks) {
    await applyNetwork(stack, networkMgr);
  }
  await router.apply(stacks);
}

async function applyNetwork(stack: any, networkMgr: NetworkManager): Promise<void> {
  const primaryService = Object.entries(stack.spec?.services || {})[0];
  if (primaryService) {
    const [serviceName, service] = primaryService as [string, any];
    await networkMgr.ensureStackNetwork({
      stackName: stack.metadata.name,
      isolate: stack.spec?.network?.isolate ?? true,
      external: stack.spec?.network?.external,
      primaryService: serviceName,
      port: service.port,
      containerName: service.containerName,
    });
  }
}

main().catch(err => {
  console.error("Fatal error:", err);
  process.exit(1);
});
