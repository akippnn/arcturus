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

function healthyCommandRunner(commands: string[][] = []) {
  return (_command: string, args: string[]): string => {
    commands.push(args);
    if (args[0] === "inspect") {
      return JSON.stringify({ Status: "running", Running: true, Pid: 4321 });
    }
    if (args.includes("getent")) {
      return "10.0.0.2 example-app-web";
    }
    return "";
  };
}

test("generates a vhost for a valid subdomain", () => {
  const config = router.generateVhost(stack("example-app", "app.example.org"));
  assert.match(config, /server_name app\.example\.org;/);
  assert.match(config, /http:\/\/example-app-web:8080/);
});

test("denies apex ownership to non-core stacks", () => {
  assert.throws(
    () => router.generateVhost(stack("example-app", "example.org")),
    /apex route denied/,
  );
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
      commandRunner: healthyCommandRunner(commands),
      pidExists: () => true,
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
    assert.equal(receipt.services["example-app"].verification.status, "passed");
    assert.equal(commands.length, 5);
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
      commandRunner: (_command, args) => {
        if (args[0] === "inspect") {
          return JSON.stringify({ Status: "running", Running: true, Pid: 4321 });
        }
        if (args.includes("nginx")) {
          throw new Error("password=should-not-leak invalid nginx");
        }
        return "";
      },
      pidExists: () => true,
    });
    await assert.rejects(failedRouter.apply([stack("example-app", "app.example.org")]));
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].status, "failed");
    assert.doesNotMatch(JSON.stringify(receipt), /should-not-leak/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("records a failed receipt when apex authorization is missing", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const statusFile = join(root, "router-status.json");
    const apexRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      commandRunner: healthyCommandRunner(),
      pidExists: () => true,
    });
    await assert.rejects(apexRouter.apply([stack("example-app", "example.org")]), /apex route denied/);
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].status, "failed");
    assert.equal(receipt.services["example-app"].configDigest, null);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("fails publication when the generated vhost is absent", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const statusFile = join(root, "router-status.json");
    const missingVhostRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      commandRunner: healthyCommandRunner(),
      pidExists: () => true,
      pathExists: path => !path.endsWith("generated-example-app.conf"),
    });
    await assert.rejects(
      missingVhostRouter.apply([stack("example-app", "app.example.org")]),
      /generated vhost verification failed/,
    );
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].status, "failed");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("fails publication when portal DNS cannot resolve the upstream", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const statusFile = join(root, "router-status.json");
    const dnsRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      commandRunner: (_command, args) => {
        if (args[0] === "inspect") {
          return JSON.stringify({ Status: "running", Running: true, Pid: 4321 });
        }
        if (args.includes("getent")) {
          throw new Error("name not found");
        }
        return "";
      },
      pidExists: () => true,
    });
    await assert.rejects(dnsRouter.apply([stack("example-app", "app.example.org")]), /runtime verification/);
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].verification.status, "failed");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("accepts a portal DNS alias owned by a live container", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const aliasRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile: join(root, "router-status.json"),
      commandRunner: (_command, args) => {
        if (args[0] === "inspect" && args.at(-1) === "example-app-web") {
          throw new Error("no such container");
        }
        if (args[0] === "ps") {
          return "container-id\n";
        }
        if (args[0] === "inspect" && args.at(-1) === "container-id") {
          return JSON.stringify({
            State: { Status: "running", Running: true, Pid: 4321 },
            NetworkSettings: {
              Networks: { internal_routing: { Aliases: ["example-app-web"] } },
            },
          });
        }
        return "";
      },
      pidExists: () => true,
    });
    await aliasRouter.apply([stack("example-app", "app.example.org")]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("fails publication when the upstream port is closed", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const portRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile: join(root, "router-status.json"),
      commandRunner: (_command, args) => {
        if (args[0] === "inspect") {
          return JSON.stringify({ Status: "running", Running: true, Pid: 4321 });
        }
        if (args.includes("nc")) {
          throw new Error("connection refused");
        }
        return "";
      },
      pidExists: () => true,
    });
    await assert.rejects(portRouter.apply([stack("example-app", "app.example.org")]), /runtime verification/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("fails publication for a running container whose PID is gone", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const ghostRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile: join(root, "router-status.json"),
      commandRunner: healthyCommandRunner(),
      pidExists: () => false,
    });
    await assert.rejects(ghostRouter.apply([stack("example-app", "app.example.org")]), /runtime verification/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("reports failed nginx restoration without losing the original failure", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-router-"));
  try {
    const statusFile = join(root, "router-status.json");
    const restorationRouter = new Router({
      vhostsDir: join(root, "vhosts"),
      nginxContainer: "portal-nginx",
      baseDomain: "example.org",
      certDomain: "example.org",
      registrySocket: join(root, "registry.sock"),
      statusFile,
      commandRunner: (_command, args) => {
        if (args[0] === "inspect") {
          return JSON.stringify({ Status: "running", Running: true, Pid: 4321 });
        }
        if (args.includes("nginx")) {
          throw new Error("nginx unavailable");
        }
        return "";
      },
      pidExists: () => true,
    });
    await assert.rejects(restorationRouter.apply([stack("example-app", "app.example.org")]), /restoration failed/);
    const receipt = JSON.parse(readFileSync(statusFile, "utf-8"));
    assert.equal(receipt.services["example-app"].verification.restoration.at(-1).status, "failed");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
