// src/schema.ts - Zod schema for arcturus.json (runtime validation)
// CUE handles compile-time; Zod handles runtime

import { z } from "zod";

const LegacyNameSchema = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,62}$/);
const LegacyRuntimeNameSchema = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/);
const LegacyMapKeySchema = z.string().min(1).max(128);
const DnsNameSchema = z.string().max(253).refine(value =>
  !value.endsWith(".") && value.split(".").every(label =>
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)
  ), "invalid DNS name");
const LegacyRevisionSchema = z.string().regex(/^[0-9a-f]{40}$/);
const LegacyDeploymentIdSchema = z.string().uuid();
const LegacyManifestDigestSchema = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const BodySizeSchema = z.string().regex(/^[1-9][0-9]{0,8}[kKmMgG]?$/);
const LegacyNginxExtrasSchema = z.string().max(4096).refine(
  value => !/[{}\r\n\0]/.test(value),
  "nginxExtras contains forbidden control characters or block delimiters",
);

export const ServiceSchema = z.object({
  port: z.number().int().min(1).max(65535),
  protocol: z.enum(["http", "https", "tcp", "udp"]).default("http"),
  domains: z.array(DnsNameSchema).max(64).optional(),
  aliases: z.array(z.string().regex(/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/)).max(64).optional(),
  type: z.enum(["proxy", "static", "tcp-forward", "udp-forward"]).default("proxy"),
  websocket: z.boolean().default(false),
  maxBodySize: BodySizeSchema.default("1G"),
  nginxExtras: LegacyNginxExtrasSchema.optional(),
  healthCheck: z.string().max(2048).optional(),
  containerName: LegacyRuntimeNameSchema.optional(),
}).strip();

export const RedirectSchema = z.object({
  from: DnsNameSchema,
  to: z.string().url().refine(value => value.startsWith("https://") || value.startsWith("http://"), "redirect must use http(s)"),
  code: z.union([z.literal(301), z.literal(302), z.literal(307), z.literal(308)]).default(301),
}).strip();

export const NetworkSchema = z.object({
  isolate: z.boolean().default(true),
  external: z.array(LegacyRuntimeNameSchema).max(32).optional(),
}).strip();

export const DeploySchema = z.object({
  managed: z.boolean().default(false),
  strategy: z.enum(["docker-compose", "quadlet"]).default("docker-compose"),
  autoUpdate: z.boolean().default(false),
  healthCheck: z.string().max(2048).optional(),
}).strip();

export const SecuritySchema = z.object({
  corsOrigins: z.array(z.string().url()).max(64).optional(),
  rateLimit: z.enum(["default", "strict", "permissive", "none"]).optional(),
}).strip();

export const StackSchema = z.object({
  apiVersion: z.literal("arcturus.u128.org/v1"),
  kind: z.literal("Stack"),
  metadata: z.object({
    name: LegacyNameSchema,
    namespace: z.string().min(1).max(128).default("default"),
    labels: z.record(z.string().max(128), z.string().max(1024)).optional(),
    annotations: z.record(z.string().max(128), z.string().max(2048)).optional(),
  }).strip(),
  spec: z.object({
    services: z.record(LegacyRuntimeNameSchema, ServiceSchema),
    redirects: z.record(LegacyMapKeySchema, RedirectSchema).optional(),
    network: NetworkSchema.optional(),
    deploy: DeploySchema.optional(),
    security: SecuritySchema.optional(),
  }).strip(),
}).strip().superRefine((stack, context) => {
  const serviceCount = Object.keys(stack.spec.services).length;
  if (serviceCount === 0) {
    context.addIssue({ code: "custom", path: ["spec", "services"], message: "at least one service is required" });
  }
  if (serviceCount > 64) {
    context.addIssue({ code: "custom", path: ["spec", "services"], message: "no more than 64 services are allowed" });
  }
  if (Object.keys(stack.spec.redirects || {}).length > 64) {
    context.addIssue({ code: "custom", path: ["spec", "redirects"], message: "no more than 64 redirects are allowed" });
  }
  const annotations = stack.metadata.annotations || {};
  const revision = annotations["arcturus.u128.org/revision"];
  const deploymentId = annotations["arcturus.u128.org/deployment-id"];
  const manifestDigest = annotations["arcturus.u128.org/manifest-digest"];
  const compatibilitySource = annotations["arcturus.u128.org/compatibility-source"];
  if (revision && !LegacyRevisionSchema.safeParse(revision).success) {
    context.addIssue({ code: "custom", path: ["metadata", "annotations", "arcturus.u128.org/revision"], message: "revision must be a lowercase 40-character Git SHA" });
  }
  if (deploymentId && !LegacyDeploymentIdSchema.safeParse(deploymentId).success) {
    context.addIssue({ code: "custom", path: ["metadata", "annotations", "arcturus.u128.org/deployment-id"], message: "deployment ID must be a UUID" });
  }
  if (manifestDigest && !LegacyManifestDigestSchema.safeParse(manifestDigest).success) {
    context.addIssue({ code: "custom", path: ["metadata", "annotations", "arcturus.u128.org/manifest-digest"], message: "manifest digest must be sha256:<64 lowercase hex>" });
  }
  if (compatibilitySource && compatibilitySource !== "v1" && compatibilitySource !== "v2") {
    context.addIssue({ code: "custom", path: ["metadata", "annotations", "arcturus.u128.org/compatibility-source"], message: "compatibility source must be v1 or v2" });
  }
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
  environment: z.record(z.string(), z.string()).default({}),
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
    context.addIssue({ code: "custom", path: ["schedule"], message: "scheduled components require schedule" });
  }
  if (component.mode !== "scheduled" && component.schedule) {
    context.addIssue({ code: "custom", path: ["schedule"], message: "schedule is only valid for scheduled components" });
  }
});

const LegacyComposeTakeoverSchema = z.object({
  project: z.string().regex(/^[a-z0-9][a-z0-9_-]{0,62}$/),
  required: z.boolean().default(false),
  cleanup: z.enum(["retain", "remove"]).default("retain"),
}).strict();

const MigrationPolicySchema = z.object({
  legacyCompose: z.array(LegacyComposeTakeoverSchema).default([]),
}).strict().superRefine((migration, context) => {
  const projects = migration.legacyCompose.map(item => item.project);
  if (new Set(projects).size !== projects.length) {
    context.addIssue({
      code: "custom",
      path: ["legacyCompose"],
      message: "legacy Compose projects must be unique",
    });
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
    components: z.record(z.string(), ReleaseComponentSchema),
    networks: z.array(z.object({
      name: z.string().regex(/^[a-z0-9][a-z0-9_-]{0,62}$/),
      external: z.boolean().default(true),
    }).strict()).default([{ name: "internal_routing", external: true }]),
    routing: z.record(z.string(), z.object({
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
    migration: MigrationPolicySchema.optional(),
  }).strict(),
}).strict();

export const ManifestSchema = z.union([StackSchema, ServiceReleaseSchema]);

export type Stack = z.infer<typeof StackSchema>;
export type Service = z.infer<typeof ServiceSchema>;
export type Redirect = z.infer<typeof RedirectSchema>;
export type ServiceRelease = z.infer<typeof ServiceReleaseSchema>;
