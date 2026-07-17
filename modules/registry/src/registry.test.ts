import assert from "node:assert/strict";
import test from "node:test";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { StackRegistry } from "./registry.js";

const manifest = {
  apiVersion: "arcturus.u128.org/v1",
  kind: "Stack",
  metadata: { name: "legacy-app" },
  spec: {
    services: {
      web: {
        port: 8080,
        domains: ["legacy.example.org"],
        containerName: "legacy-app",
      },
    },
  },
};

test("native manifest-v1 routing receives registry-owned provenance", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-registry-"));
  try {
    const stackDir = join(root, "legacy-app");
    mkdirSync(stackDir);
    const path = join(stackDir, "arcturus.json");
    writeFileSync(path, JSON.stringify(manifest, null, 2) + "\n");

    const registry = new StackRegistry(root);
    await registry.scan();
    const stack = registry.get("legacy-app");
    assert.ok(stack);
    assert.equal(
      stack.metadata.annotations?.["arcturus.u128.org/compatibility-source"],
      "v1",
    );
    const digest = stack.metadata.annotations?.["arcturus.u128.org/manifest-digest"] || "";
    const revision = stack.metadata.annotations?.["arcturus.u128.org/revision"] || "";
    assert.match(digest, /^sha256:[0-9a-f]{64}$/);
    assert.equal(revision, digest.slice("sha256:".length, "sha256:".length + 40));

    const persisted = JSON.parse(readFileSync(path, "utf-8"));
    assert.equal(
      persisted.metadata.annotations?.["arcturus.u128.org/compatibility-source"],
      undefined,
    );
    assert.equal(
      persisted.metadata.annotations?.["arcturus.u128.org/manifest-digest"],
      undefined,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("authored manifest-v1 provenance cannot spoof v2", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-registry-"));
  try {
    const stackDir = join(root, "legacy-app");
    mkdirSync(stackDir);
    const path = join(stackDir, "arcturus.json");
    writeFileSync(path, JSON.stringify({
      ...manifest,
      metadata: {
        name: "legacy-app",
        annotations: {
          "arcturus.u128.org/compatibility-source": "v2",
          "arcturus.u128.org/manifest-digest": "stale-digest",
          "arcturus.u128.org/revision": "main",
          "arcturus.u128.org/deployment-id": "stale-deployment",
        },
      },
    }, null, 2) + "\n");

    const registry = new StackRegistry(root);
    await registry.scan();
    const stack = registry.get("legacy-app");
    assert.ok(stack);
    assert.equal(
      stack.metadata.annotations?.["arcturus.u128.org/compatibility-source"],
      "v1",
    );
    const digest = stack.metadata.annotations?.["arcturus.u128.org/manifest-digest"] || "";
    assert.match(digest, /^sha256:[0-9a-f]{64}$/);
    assert.equal(
      stack.metadata.annotations?.["arcturus.u128.org/revision"],
      digest.slice("sha256:".length, "sha256:".length + 40),
    );
    assert.equal(
      stack.metadata.annotations?.["arcturus.u128.org/deployment-id"],
      undefined,
    );
    const persisted = JSON.parse(readFileSync(path, "utf-8"));
    for (const key of [
      "arcturus.u128.org/compatibility-source",
      "arcturus.u128.org/manifest-digest",
      "arcturus.u128.org/revision",
      "arcturus.u128.org/deployment-id",
    ]) {
      assert.equal(persisted.metadata.annotations?.[key], undefined);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});


test("native manifest-v1 provenance is invariant to service key order", async () => {
  const roots = [
    mkdtempSync(join(tmpdir(), "arcturus-registry-order-a-")),
    mkdtempSync(join(tmpdir(), "arcturus-registry-order-b-")),
  ];
  try {
    const manifests = [
      {
        ...manifest,
        spec: {
          services: {
            api: { port: 8081, domains: ["api.example.org"], containerName: "legacy-api" },
            web: { port: 8080, domains: ["legacy.example.org"], containerName: "legacy-web" },
          },
        },
      },
      {
        ...manifest,
        spec: {
          services: {
            web: { port: 8080, domains: ["legacy.example.org"], containerName: "legacy-web" },
            api: { port: 8081, domains: ["api.example.org"], containerName: "legacy-api" },
          },
        },
      },
    ];
    const digests: string[] = [];
    for (let index = 0; index < roots.length; index += 1) {
      const stackDir = join(roots[index], "legacy-app");
      mkdirSync(stackDir);
      writeFileSync(join(stackDir, "arcturus.json"), JSON.stringify(manifests[index], null, 2) + "\n");
      const registry = new StackRegistry(roots[index]);
      await registry.scan();
      const stack = registry.get("legacy-app");
      assert.ok(stack);
      digests.push(stack.metadata.annotations?.["arcturus.u128.org/manifest-digest"] || "");
    }
    assert.equal(digests[0], digests[1]);
  } finally {
    for (const root of roots) rmSync(root, { recursive: true, force: true });
  }
});


test("manifest-v1 service count is bounded", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-registry-count-"));
  try {
    const stackDir = join(root, "legacy-app");
    mkdirSync(stackDir);
    const services = Object.fromEntries(
      Array.from({ length: 65 }, (_, index) => [
        `service-${index}`,
        { port: 8000 + index, containerName: `legacy-${index}` },
      ]),
    );
    writeFileSync(join(stackDir, "arcturus.json"), JSON.stringify({
      ...manifest,
      spec: { services },
    }, null, 2) + "\n");
    const registry = new StackRegistry(root);
    await registry.scan();
    assert.equal(registry.get("legacy-app"), undefined);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});


test("unknown legacy fields are stripped before routing", async () => {
  const root = mkdtempSync(join(tmpdir(), "arcturus-registry-unknown-"));
  try {
    const stackDir = join(root, "legacy-app");
    mkdirSync(stackDir);
    const path = join(stackDir, "arcturus.json");
    writeFileSync(path, JSON.stringify({
      ...manifest,
      legacyTopLevel: "ignored",
      spec: {
        ...manifest.spec,
        legacySpecField: { dangerous: true },
        services: {
          web: {
            ...manifest.spec.services.web,
            legacyDirective: "proxy_set_header X-Injected true;",
          },
        },
      },
    }, null, 2) + "\n");
    const registry = new StackRegistry(root);
    await registry.scan();
    const stack = registry.get("legacy-app") as any;
    assert.ok(stack);
    assert.equal(stack.legacyTopLevel, undefined);
    assert.equal(stack.spec.legacySpecField, undefined);
    assert.equal(stack.spec.services.web.legacyDirective, undefined);
    const persisted = JSON.parse(readFileSync(path, "utf-8"));
    assert.equal(persisted.legacyTopLevel, undefined);
    assert.equal(persisted.spec.legacySpecField, undefined);
    assert.equal(persisted.spec.services.web.legacyDirective, undefined);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
