import assert from "node:assert/strict";
import test from "node:test";

import { ServiceReleaseSchema, StackSchema } from "./schema.js";

const digest = `registry.example.org/apps/web@sha256:${"a".repeat(64)}`;

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
  const result = ServiceReleaseSchema.safeParse({
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
    },
  });

  assert.equal(result.success, true, result.success ? undefined : result.error.message);
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
