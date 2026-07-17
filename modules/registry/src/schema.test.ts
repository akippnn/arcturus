import assert from "node:assert/strict";
import test from "node:test";

import { ServiceReleaseSchema, StackSchema } from "./schema.js";

const digest = `registry.example.org/apps/web@sha256:${"a".repeat(64)}`;

function release(overrides: Record<string, unknown> = {}) {
  return {
    apiVersion: "arcturus.u128.org/v2",
    kind: "ServiceRelease",
    metadata: {
      name: "example-service",
      revision: "b".repeat(40),
    },
    spec: {
      components: {
        web: {
          image: digest,
          environment: { NODE_ENV: "production" },
          ports: [{ container: 3000 }],
        },
      },
      routing: {
        web: {
          component: "web",
          port: 3000,
          domains: ["example.org"],
        },
      },
      ...overrides,
    },
  };
}

test("StackSchema accepts string-keyed service and metadata records", () => {
  const result = StackSchema.safeParse({
    apiVersion: "arcturus.u128.org/v1",
    kind: "Stack",
    metadata: {
      name: "example-service",
      labels: { environment: "test" },
      annotations: { owner: "platform" },
    },
    spec: {
      services: {
        web: { port: 3000 },
      },
    },
  });

  assert.equal(result.success, true, result.success ? undefined : result.error.message);
});

test("ServiceReleaseSchema accepts component, environment, and routing records", () => {
  const result = ServiceReleaseSchema.safeParse(release());
  assert.equal(result.success, true, result.success ? undefined : result.error.message);
});

test("ServiceReleaseSchema accepts legacy Compose migration policy", () => {
  const result = ServiceReleaseSchema.safeParse(release({
    migration: {
      legacyCompose: [
        { project: "stellar-project", required: true, cleanup: "remove" },
      ],
    },
  }));

  assert.equal(result.success, true, result.success ? undefined : result.error.message);
  if (result.success) {
    assert.equal(result.data.spec.migration?.legacyCompose[0]?.project, "stellar-project");
  }
});

test("ServiceReleaseSchema rejects duplicate legacy Compose projects", () => {
  const result = ServiceReleaseSchema.safeParse(release({
    migration: {
      legacyCompose: [
        { project: "stellar-project" },
        { project: "stellar-project" },
      ],
    },
  }));

  assert.equal(result.success, false);
  if (!result.success) {
    assert.deepEqual(result.error.issues[0]?.path, ["spec", "migration", "legacyCompose"]);
  }
});

test("scheduled components require a schedule", () => {
  const result = ServiceReleaseSchema.safeParse({
    apiVersion: "arcturus.u128.org/v2",
    kind: "ServiceRelease",
    metadata: {
      name: "example-service",
      revision: "c".repeat(40),
    },
    spec: {
      components: {
        cleanup: {
          image: digest,
          mode: "scheduled",
        },
      },
    },
  });

  assert.equal(result.success, false);
  if (!result.success) {
    assert.deepEqual(result.error.issues[0]?.path, ["spec", "components", "cleanup", "schedule"]);
  }
});
