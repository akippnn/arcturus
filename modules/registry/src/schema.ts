// src/schema.ts - Zod schema for arcturus.json (runtime validation)
// CUE handles compile-time; Zod handles runtime

import { z } from "zod";

export const ServiceSchema = z.object({
  port: z.number().int().min(1).max(65535),
  protocol: z.enum(["http", "https", "tcp", "udp"]).default("http"),
  domains: z.array(z.string()).optional(),
  aliases: z.array(z.string()).optional(),
  type: z.enum(["proxy", "static", "tcp-forward", "udp-forward"]).default("proxy"),
  websocket: z.boolean().default(false),
  maxBodySize: z.string().default("1G"),
  nginxExtras: z.string().optional(),
  healthCheck: z.string().optional(),
  containerName: z.string().optional(),
});

export const RedirectSchema = z.object({
  from: z.string(),
  to: z.string(),
  code: z.number().int().min(300).max(399).default(301),
});

export const NetworkSchema = z.object({
  isolate: z.boolean().default(true),
  external: z.array(z.string()).optional(),
});

export const DeploySchema = z.object({
  managed: z.boolean().default(false),
  strategy: z.enum(["docker-compose", "quadlet"]).default("docker-compose"),
  autoUpdate: z.boolean().default(false),
  healthCheck: z.string().optional(),
});

export const SecuritySchema = z.object({
  corsOrigins: z.array(z.string()).optional(),
  rateLimit: z.enum(["default", "strict", "permissive", "none"]).optional(),
});

export const StackSchema = z.object({
  apiVersion: z.literal("arcturus.u128.org/v1"),
  kind: z.literal("Stack"),
  metadata: z.object({
    name: z.string().regex(/^[a-z0-9-]+$/),
    namespace: z.string().default("default"),
    labels: z.record(z.string()).optional(),
    annotations: z.record(z.string()).optional(),
  }),
  spec: z.object({
    services: z.record(ServiceSchema),
    redirects: z.record(RedirectSchema).optional(),
    network: NetworkSchema.optional(),
    deploy: DeploySchema.optional(),
    security: SecuritySchema.optional(),
  }),
});

const DigestImageSchema = z.string().regex(
  /^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:\/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$/,
  "image must be fully qualified and pinned to a sha256 digest",
);

const SecretRefSchema = z.object({
  name: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$/),
  target: z.string().nullable().optional(),
  type: z.enum(["file", "env"]).default("file"),
}).strict();

const VolumeRefSchema = z.object({
  source: z.string().regex(/^(?:\/[^\n]*|[a-z0-9][a-z0-9_-]{0,62})$/),
  target: z.string().startsWith("/"),
  type: z.enum(["bind", "volume"]).default("bind"),
  readOnly: z.boolean().default(false),
  external: z.boolean().default(true),
  selinuxRelabel: z.enum(["private", "shared"]).nullable().optional(),
}).strict();

const ReleaseComponentSchema = z.object({
  image: DigestImageSchema,
  containerName: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/).nullable().optional(),
  mode: z.enum(["service", "oneshot", "scheduled"]).default("service"),
  command: z.array(z.string()).default([]),
  environment: z.record(z.string()).default({}),
  secrets: z.array(SecretRefSchema).default([]),
  ports: z.array(z.object({
    container: z.number().int().min(1).max(65535),
    host: z.number().int().min(1).max(65535).nullable().optional(),
    hostIp: z.string().nullable().optional(),
    protocol: z.enum(["tcp", "udp"]).default("tcp"),
  }).strict()).default([]),
  volumes: z.array(VolumeRefSchema).default([]),
  dependsOn: z.array(z.string()).default([]),
  networks: z.array(z.string()).default(["internal_routing"]),
  healthCheck: z.object({
    command: z.string().min(1),
    interval: z.string().default("10s"),
    timeout: z.string().default("5s"),
    retries: z.number().int().min(1).max(30).default(5),
    startPeriod: z.string().default("10s"),
  }).strict().nullable().optional(),
  schedule: z.object({
    onCalendar: z.string().min(1),
    persistent: z.boolean().default(true),
    randomizedDelaySeconds: z.number().int().min(0).max(86400).default(0),
    runOnDeploy: z.boolean().default(false),
  }).strict().nullable().optional(),
  restart: z.enum(["always", "on-failure", "no"]).default("always"),
}).strict().superRefine((component, context) => {
  if (component.mode === "scheduled" && !component.schedule) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["schedule"], message: "scheduled components require schedule" });
  }
  if (component.mode !== "scheduled" && component.schedule) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["schedule"], message: "schedule is only valid for scheduled components" });
  }
});

export const ServiceReleaseSchema = z.object({
  apiVersion: z.literal("arcturus.u128.org/v2"),
  kind: z.literal("ServiceRelease"),
  metadata: z.object({
    name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/),
    revision: z.string().regex(/^[0-9a-f]{40}$/),
    deploymentId: z.string().uuid().optional(),
  }).strict(),
  spec: z.object({
    components: z.record(ReleaseComponentSchema),
    networks: z.array(z.object({
      name: z.string().regex(/^[a-z0-9][a-z0-9_-]{0,62}$/),
      external: z.boolean().default(true),
    }).strict()).default([{ name: "internal_routing", external: true }]),
    routing: z.record(z.object({
      component: z.string(),
      port: z.number().int().min(1).max(65535),
      protocol: z.enum(["http", "https", "tcp", "udp"]).default("http"),
      domains: z.array(z.string()).default([]),
      aliases: z.array(z.string()).default([]),
      websocket: z.boolean().default(false),
      maxBodySize: z.string().default("1G"),
    }).strict()).default({}),
    deployment: z.object({
      timeoutSeconds: z.number().int().min(10).max(1800).default(300),
      rollbackOnFailure: z.boolean().default(true),
    }).strict().default({ timeoutSeconds: 300, rollbackOnFailure: true }),
  }).strict(),
}).strict();

export const ManifestSchema = z.union([StackSchema, ServiceReleaseSchema]);

export type Stack = z.infer<typeof StackSchema>;
export type Service = z.infer<typeof ServiceSchema>;
export type Redirect = z.infer<typeof RedirectSchema>;
export type ServiceRelease = z.infer<typeof ServiceReleaseSchema>;
