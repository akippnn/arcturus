import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Router } from "./router.js";
import { NetworkManager } from "./network.js";

const router = new Router({
  vhostsDir: ".",
  nginxContainer: "portal-nginx",
  baseDomain: "example.org",
  certDomain: "example.org",
  registrySocket: "/tmp/registry.sock",
});

function stack(name: string, domain: string) {
  return {
    metadata: { name },
    spec: {
      services: {
        web: {
          port: 8080,
          domains: [domain],
        },
      },
    },
  };
}

test("generates a vhost for a valid subdomain", () => {
  const config = router.generateVhost(stack("example-app", "app.example.org"));
  assert.match(config, /server_name app\.example\.org;/);
  assert.match(config, /http:\/\/example-app-web:8080/);
});

test("denies apex ownership to non-core stacks", () => {
  const config = router.generateVhost(stack("example-app", "example.org"));
  assert.equal(config, "");
});

test("rejects path traversal in stack names", () => {
  assert.throws(
    () => router.generateVhost(stack("../outside", "app.example.org")),
    /Invalid stack name/,
  );
});

test("rejects nginx directive injection through domains", () => {
  assert.throws(
    () => router.generateVhost(stack("example-app", "app.example.org; include /tmp/x")),
    /Invalid domain/,
  );
});

test("rejects arbitrary container engine commands", () => {
  assert.throws(() => new NetworkManager("podman; id"), /Unsupported container engine/);
});

test("network reconciliation does not reconnect existing memberships", async () => {
  const commands: string[][] = [];
  const memberships: Record<string, Set<string>> = {
    "portal-nginx": new Set(["arcturus-example-app"]),
    "example-app-web": new Set(["arcturus-example-app", "internal_routing"]),
  };
  const manager = new NetworkManager("podman", "portal-nginx", (_command, args) => {
    commands.push(args);
    if (args[0] === "container" && args[1] === "inspect" && args[2] === "--format") {
      const container = args[4];
      return JSON.stringify(Object.fromEntries(
        Array.from(memberships[container] || []).map(network => [network, {}]),
      ));
    }
    return "{}";
  });

  await manager.ensureStackNetwork({
    stackName: "example-app",
    isolate: true,
    external: ["internal_routing"],
    primaryService: "web",
    port: 8080,
  });

  assert.equal(commands.filter(args => args[0] === "network" && args[1] === "connect").length, 0);
});

test("network reconciliation connects missing memberships only once", async () => {
  const commands: string[][] = [];
  const memberships: Record<string, Set<string>> = {
    "portal-nginx": new Set(),
    "example-app-web": new Set(),
  };
  const manager = new NetworkManager("podman", "portal-nginx", (_command, args) => {
    commands.push(args);
    if (args[0] === "container" && args[1] === "inspect" && args[2] === "--format") {
      const container = args[4];
      return JSON.stringify(Object.fromEntries(
        Array.from(memberships[container] || []).map(network => [network, {}]),
      ));
    }
    if (args[0] === "network" && args[1] === "connect") {
      const network = args[2];
      const container = args[3];
      memberships[container] ||= new Set();
      memberships[container].add(network);
    }
    return "{}";
  });

  const config = {
    stackName: "example-app",
    isolate: true,
    external: ["internal_routing"],
    primaryService: "web",
    port: 8080,
  };
  await manager.ensureStackNetwork(config);
  await manager.ensureStackNetwork(config);

  const connects = commands.filter(args => args[0] === "network" && args[1] === "connect");
  assert.deepEqual(connects, [
    ["network", "connect", "arcturus-example-app", "portal-nginx"],
    ["network", "connect", "arcturus-example-app", "example-app-web"],
    ["network", "connect", "internal_routing", "example-app-web"],
  ]);
});

test("publishes a revision-matched routing receipt after nginx reload", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  const commands: string[][] = [];
  try {
    const statusFile = join(root, "router-status.json");
    const receiptRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      containerCli: "podman",
      commandRunner: (_command, args) => commands.push(args),
    });
    const release = stack("example-app", "app.example.org");
    release.metadata = {
      ...release.metadata,
      annotations: {
        "arcturus.u128.org/revision": "1".repeat(40),
        "arcturus.u128.org/deployment-id": "11111111-1111-4111-8111-111111111111",
      },
    } as typeof release.metadata;
    await receiptRouter.apply([release]);
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].status, "published");
    assert.equal(receipt.services["example-app"].revision, "1".repeat(40));
    assert.equal(
      receipt.services["example-app"].deploymentId,
      "11111111-1111-4111-8111-111111111111",
    );
    assert.deepEqual(receipt.services["example-app"].upstreams, ["example-app-web:8080"]);
    assert.match(receipt.services["example-app"].configDigest, /^sha256:[0-9a-f]{64}$/);
    assert.equal(commands.length, 2);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("restores nginx configuration and records a redacted failure", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const statusFile = join(root, "router-status.json");
    const failedRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      commandRunner: () => { throw new Error("password=should-not-leak invalid nginx"); },
    });
    await assert.rejects(failedRouter.apply([stack("example-app", "app.example.org")]));
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].status, "failed");
    assert.doesNotMatch(JSON.stringify(receipt), /should-not-leak/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
