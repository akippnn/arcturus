// src/index.ts - Router module entry point

import { Router } from "./router.js";
import { NetworkManager } from "./network.js";
import { BusClient } from "./bus-client.js";

class ConfigurationError extends Error {
  readonly exitCode = 78;
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new ConfigurationError(`${name} must be configured in platform.env`);
  }
  return value;
}

function configuredDomain(name: string, fallback?: string): string {
  const value = process.env[name]?.trim() || fallback;
  if (!value) {
    throw new ConfigurationError(`${name} must be configured in platform.env`);
  }
  if (value === "example.org" && process.env.ARCTURUS_ALLOW_RESERVED_DOMAIN !== "1") {
    throw new ConfigurationError(
      `${name}=example.org is a reserved placeholder; configure the real certificate domain`,
    );
  }
  return value;
}

function loadConfig() {
  const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.()}`;
  const baseDomain = configuredDomain("BASE_DOMAIN");

  return {
    vhostsDir: requiredEnvironment("VHOSTS_DIR"),
    nginxContainer: process.env.NGINX_CONTAINER?.trim() || "portal-nginx",
    baseDomain,
    certDomain: configuredDomain("CERT_DOMAIN", baseDomain),
    apexService: process.env.ARCTURUS_APEX_SERVICE || undefined,
    registrySocket: process.env.REGISTRY_SOCKET || `${runtimeDir}/arcturus/registry.sock`,
    busSocket: process.env.BUS_SOCKET || `${runtimeDir}/arcturus/bus.sock`,
    statusFile: process.env.ROUTER_STATUS_FILE || `${runtimeDir}/arcturus/router-status.json`,
    containerCli: (process.env.CONTAINER_CLI || "podman") as "podman" | "docker",
  };
}

async function main() {
  const config = loadConfig();

  console.log("Arcturus Router starting...");
  console.log("Vhosts dir:", config.vhostsDir);
  console.log("Base domain:", config.baseDomain);
  console.log("Certificate domain:", config.certDomain);

  const router = new Router(config);
  const networkMgr = new NetworkManager(config.containerCli, config.nginxContainer);
  let reapplyQueue = Promise.resolve();

  const queueFullReapply = (): Promise<void> => {
    const next = reapplyQueue.catch(() => undefined).then(() => fullReapply(router, networkMgr));
    reapplyQueue = next;
    return next;
  };

  // Connect to message bus
  const bus = new BusClient({ socketPath: config.busSocket, clientName: "router" });
  try {
    await bus.connect();
    console.log("Connected to message bus");

    // Registry events may arrive in bursts. Queue a complete reconciliation so
    // network and nginx mutations never overlap.
    bus.on("stack.discovered", async (payload) => {
      console.log("[Bus] Stack discovered:", payload.stackName);
      await queueFullReapply();
    });

    bus.on("stack.updated", async (payload) => {
      console.log("[Bus] Stack updated:", payload.stackName);
      await queueFullReapply();
    });

    bus.on("stack.removed", async (payload) => {
      console.log("[Bus] Stack removed:", payload.stackName);
      await router.removeVhost(payload.stackName);
      await networkMgr.removeStackNetwork(payload.stackName);
      await queueFullReapply();
    });
  } catch {
    console.warn("Message bus not available, falling back to polling");
  }

  // Initial sync
  try {
    await queueFullReapply();
    console.log("Nginx configs generated");
  } catch (err) {
    console.error("Initial sync failed:", (err as Error).message);
  }

  setInterval(() => {
    queueFullReapply().catch((err) => {
      console.error("Sync failed:", (err as Error).message);
    });
  }, 30000);
  console.log("Router polling every 30s");
}

async function fullReapply(router: Router, networkMgr: NetworkManager): Promise<void> {
  const stacks = await router.fetchStacks();

  // Establish reachability first. A route must never be published before nginx
  // can reach its upstream network.
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
  console.error("Fatal error:", (err as Error).message);
  process.exit(err instanceof ConfigurationError ? err.exitCode : 1);
});
