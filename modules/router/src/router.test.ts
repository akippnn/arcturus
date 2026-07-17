import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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

function stack(name: string, domain: string): any {
  return {
    metadata: {
      name,
      annotations: {
        "arcturus.u128.org/revision": "1".repeat(40),
        "arcturus.u128.org/compatibility-source": "v2",
      },
    },
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



test("enforcement rejects routing stacks without registry provenance", () => {
  const unsafe = stack("legacy-app", "legacy.example.org");
  unsafe.metadata = { name: "legacy-app" };
  assert.throws(
    () => router.generateVhost(unsafe),
    /lacks verified v1 manifest provenance or v2 release provenance/,
  );
});

function registryStampedV1(name: string, domain: string): any {
  const native = stack(name, domain);
  native.metadata = { name };
  const digestHex = createHash("sha256").update(JSON.stringify(native)).digest("hex");
  native.metadata.annotations = {
    "arcturus.u128.org/compatibility-source": "v1",
    "arcturus.u128.org/manifest-digest": `sha256:${digestHex}`,
    "arcturus.u128.org/revision": digestHex.slice(0, 40),
  };
  return native;
}

test("enforcement accepts registry-stamped native manifest-v1 routing", () => {
  const native = registryStampedV1("legacy-app", "legacy.example.org");
  assert.match(router.generateVhost(native), /legacy\.example\.org/);
});

test("enforcement rejects tampered native manifest-v1 routing", () => {
  const native = registryStampedV1("legacy-app", "legacy.example.org");
  native.spec.services.web.domains = ["tampered.example.org"];
  assert.throws(
    () => router.generateVhost(native),
    /lacks verified v1 manifest provenance or v2 release provenance/,
  );
});

test("audit mode temporarily accepts standalone manifest-v1 stacks", () => {
  const auditRouter = new Router({
    vhostsDir: ".",
    nginxContainer: "portal-nginx",
    baseDomain: "example.org",
    certDomain: "example.org",
    registrySocket: "/tmp/registry.sock",
    legacyV1Mode: "audit",
  });
  const unsafe = stack("legacy-app", "legacy.example.org");
  unsafe.metadata = { name: "legacy-app" };
  assert.match(auditRouter.generateVhost(unsafe), /legacy\.example\.org/);
});

test("legacy nginxExtras are disabled unless explicitly enabled", () => {
  const value = stack("example-app", "app.example.org");
  value.spec.services.web.nginxExtras = "proxy_hide_header X-Test;";
  assert.throws(() => router.generateVhost(value), /nginxExtras is disabled/);
});

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
        "arcturus.u128.org/compatibility-source": "v2",
        "arcturus.u128.org/deployment-id": "11111111-1111-4111-8111-111111111111",
      },
    };
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
