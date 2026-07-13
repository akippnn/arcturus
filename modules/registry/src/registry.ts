// src/registry.ts - Core registry logic

import { readFileSync, writeFileSync, watch, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import type { Stack, Service, Redirect, ServiceRelease } from "./schema.js";
import { ManifestSchema, StackSchema } from "./schema.js";

// Standard field order for JSON normalization
const METADATA_ORDER = ["name", "namespace", "labels", "annotations"];
const SERVICE_ORDER = ["port", "protocol", "domains", "aliases", "type", "websocket", "maxBodySize", "nginxExtras", "healthCheck", "containerName"];
const REDIRECT_ORDER = ["from", "to", "code"];
const NETWORK_ORDER = ["isolate", "external"];
const DEPLOY_ORDER = ["managed", "strategy", "autoUpdate", "healthCheck"];
const SECURITY_ORDER = ["corsOrigins", "rateLimit"];

function sortObjectKeys<T extends Record<string, any>>(obj: T, order: string[]): T {
  const sorted: Record<string, any> = {};
  for (const key of order) {
    if (key in obj) sorted[key] = obj[key];
  }
  // Add any keys not in the order at the end
  for (const key of Object.keys(obj)) {
    if (!(key in sorted)) sorted[key] = obj[key];
  }
  return sorted as T;
}

function normalizeService(service: Service): Service {
  const normalized: any = {
    port: service.port,
    protocol: service.protocol,
  };
  if (service.domains?.length) normalized.domains = service.domains;
  if (service.aliases?.length) normalized.aliases = service.aliases;
  normalized.type = service.type;
  if (service.websocket) normalized.websocket = true;
  normalized.maxBodySize = service.maxBodySize || "1G";
  if (service.nginxExtras) normalized.nginxExtras = service.nginxExtras;
  if (service.healthCheck) normalized.healthCheck = service.healthCheck;
  if (service.containerName) normalized.containerName = service.containerName;
  return sortObjectKeys(normalized, SERVICE_ORDER);
}

function normalizeRedirect(redirect: Redirect): Redirect {
  const normalized: any = {
    from: redirect.from,
    to: redirect.to,
  };
  if (redirect.code && redirect.code !== 301) normalized.code = redirect.code;
  return sortObjectKeys(normalized, REDIRECT_ORDER);
}

function normalizeStack(stack: Stack): Stack {
  const normalized: any = {
    apiVersion: "arcturus.u128.org/v1",
    kind: "Stack",
    metadata: sortObjectKeys(
      {
        name: stack.metadata.name,
        namespace: stack.metadata.namespace || "default",
        ...(stack.metadata.labels ? { labels: sortObjectKeys(stack.metadata.labels, Object.keys(stack.metadata.labels).sort()) } : {}),
        ...(stack.metadata.annotations ? { annotations: sortObjectKeys(stack.metadata.annotations, Object.keys(stack.metadata.annotations).sort()) } : {}),
      },
      METADATA_ORDER
    ),
    spec: {},
  };

  // Services
  const services: Record<string, any> = {};
  for (const [name, svc] of Object.entries(stack.spec.services)) {
    services[name] = normalizeService(svc);
  }
  normalized.spec.services = sortObjectKeys(services, Object.keys(services));

  // Redirects
  if (stack.spec.redirects && Object.keys(stack.spec.redirects).length > 0) {
    const redirects: Record<string, any> = {};
    for (const [name, redir] of Object.entries(stack.spec.redirects)) {
      redirects[name] = normalizeRedirect(redir);
    }
    normalized.spec.redirects = sortObjectKeys(redirects, Object.keys(redirects));
  }

  // Network
  normalized.spec.network = sortObjectKeys(
    {
      isolate: stack.spec.network?.isolate ?? true,
      ...(stack.spec.network?.external?.length ? { external: stack.spec.network.external } : {}),
    },
    NETWORK_ORDER
  );

  // Deploy
  normalized.spec.deploy = sortObjectKeys(
    {
      managed: stack.spec.deploy?.managed ?? false,
      strategy: stack.spec.deploy?.strategy || "docker-compose",
      autoUpdate: stack.spec.deploy?.autoUpdate ?? false,
      ...(stack.spec.deploy?.healthCheck ? { healthCheck: stack.spec.deploy.healthCheck } : {}),
    },
    DEPLOY_ORDER
  );

  // Security
  normalized.spec.security = sortObjectKeys(
    {
      ...(stack.spec.security?.corsOrigins?.length ? { corsOrigins: stack.spec.security.corsOrigins } : {}),
      ...(stack.spec.security?.rateLimit ? { rateLimit: stack.spec.security.rateLimit } : {}),
    },
    SECURITY_ORDER
  );

  return normalized;
}

export interface RegistryEvent {
  type: "discovered" | "updated" | "removed";
  stackName: string;
  stack?: Stack;
  timestamp: number;
}

type EventListener = (event: RegistryEvent) => void;

export class StackRegistry {
  private stacks = new Map<string, Stack>();
  private listeners: EventListener[] = [];
  private baseDirs: string[];
  private manifestPattern: string;

  constructor(baseDir: string, manifestPattern = "*/arcturus.json", additionalDirs: string[] = []) {
    this.baseDirs = [baseDir, ...additionalDirs];
    this.manifestPattern = manifestPattern;
  }

  /** Subscribe to registry events */
  onEvent(listener: EventListener): () => void {
    this.listeners.push(listener);
    return () => {
      const idx = this.listeners.indexOf(listener);
      if (idx >= 0) this.listeners.splice(idx, 1);
    };
  }

  private emit(event: RegistryEvent) {
    for (const listener of this.listeners) {
      try { listener(event); } catch { /* ignore */ }
    }
  }

  /** Recursively find all arcturus.json files */
  private findManifests(dir: string, depth = 0): string[] {
    if (depth > 2) return []; // Only scan 2 levels deep
    const results: string[] = [];
    try {
      for (const entry of readdirSync(dir)) {
        const fullPath = join(dir, entry);
        const stat = statSync(fullPath);
        if (stat.isDirectory() && !entry.startsWith(".") && entry !== "modules") {
          const manifestPath = join(fullPath, "arcturus.json");
          try {
            statSync(manifestPath);
            results.push(manifestPath);
          } catch { /* no manifest */ }
          results.push(...this.findManifests(fullPath, depth + 1));
        }
      }
    } catch { /* ignore unreadable dirs */ }
    return results;
  }

  /** Load all manifests from disk */
  async scan(): Promise<void> {
    const selected = new Map<string, string>();
    for (const baseDir of this.baseDirs) {
      for (const file of this.findManifests(baseDir)) {
        selected.set(dirname(file).split("/").pop()!, file);
      }
    }
    const found = new Set<string>();

    for (const [stackName, file] of selected) {
      const stack = this.loadManifest(file);
      if (!stack) continue;

      found.add(stackName);
      const existing = this.stacks.get(stackName);

      if (!existing) {
        this.stacks.set(stackName, stack);
        this.emit({ type: "discovered", stackName, stack, timestamp: Date.now() });
      } else if (JSON.stringify(existing) !== JSON.stringify(stack)) {
        this.stacks.set(stackName, stack);
        this.emit({ type: "updated", stackName, stack, timestamp: Date.now() });
      }
    }

    // Detect removals
    for (const [name] of this.stacks) {
      if (!found.has(name)) {
        const stack = this.stacks.get(name);
        this.stacks.delete(name);
        this.emit({ type: "removed", stackName: name, stack, timestamp: Date.now() });
      }
    }
  }

  /** Load and validate a single manifest file, normalizing and writing back if needed */
  private loadManifest(path: string): Stack | null {
    try {
      const raw = readFileSync(path, "utf-8");
      const json = JSON.parse(raw);
      const result = ManifestSchema.safeParse(json);
      if (!result.success) {
        console.error(`Invalid manifest ${path}:`, result.error.format());
        return null;
      }

      if (result.data.apiVersion === "arcturus.u128.org/v2") {
        return releaseToRoutingStack(result.data);
      }
      const normalized = normalizeStack(result.data);
      const normalizedRaw = JSON.stringify(normalized, null, 2) + "\n";
      if (normalizedRaw !== raw) {
        writeFileSync(path, normalizedRaw, "utf-8");
        console.log(`Normalized manifest: ${path}`);
      }

      return normalized;
    } catch (err) {
      console.error(`Failed to load manifest ${path}:`, (err as Error).message);
      return null;
    }
  }

  /** Get a stack by name */
  get(name: string): Stack | undefined {
    return this.stacks.get(name);
  }

  /** List all stacks */
  list(): Stack[] {
    return Array.from(this.stacks.values());
  }

  /** Find stack by domain */
  findByDomain(domain: string): { stack: Stack; serviceName: string; service: Stack["spec"]["services"][string] } | null {
    for (const stack of this.stacks.values()) {
      for (const [serviceName, service] of Object.entries(stack.spec.services)) {
        if (service.domains?.includes(domain)) {
          return { stack, serviceName, service };
        }
      }
    }
    return null;
  }

  /** Find stack by alias */
  findByAlias(alias: string, baseDomain: string): { stack: Stack; serviceName: string } | null {
    const fullDomain = `${alias}.${baseDomain}`;
    return this.findByDomain(fullDomain)?.stack ? {
      stack: this.findByDomain(fullDomain)!.stack,
      serviceName: this.findByDomain(fullDomain)!.serviceName
    } : null;
  }

  /** Watch for manifest changes */
  watch(): void {
    for (const baseDir of this.baseDirs) {
      try {
        watch(baseDir, { recursive: true }, (_eventType, filename) => {
          if (filename?.endsWith("arcturus.json")) {
            setTimeout(() => this.scan(), 100);
          }
        });
        console.log(`Watching ${baseDir} for arcturus.json changes...`);
      } catch {
        console.warn(`Unable to watch manifest directory: ${baseDir}`);
      }
    }
  }
}

function releaseToRoutingStack(release: ServiceRelease): Stack {
  const services: Record<string, Service> = {};
  for (const [name, route] of Object.entries(release.spec.routing)) {
    const component = release.spec.components[route.component];
    services[name] = {
      port: route.port,
      protocol: route.protocol,
      domains: route.domains,
      aliases: route.aliases,
      type: route.protocol === "tcp" ? "tcp-forward" : route.protocol === "udp" ? "udp-forward" : "proxy",
      websocket: route.websocket,
      maxBodySize: route.maxBodySize,
      containerName: component.containerName || `arcturus-${release.metadata.name}-${route.component}`,
      healthCheck: component.healthCheck?.command,
    };
  }
  return StackSchema.parse({
    apiVersion: "arcturus.u128.org/v1",
    kind: "Stack",
    metadata: {
      name: release.metadata.name,
      namespace: "default",
      annotations: {
        "arcturus.u128.org/revision": release.metadata.revision,
        ...(release.metadata.deploymentId
          ? { "arcturus.u128.org/deployment-id": release.metadata.deploymentId }
          : {}),
      },
    },
    spec: {
      services,
      network: { isolate: false, external: release.spec.networks.map(network => network.name) },
      deploy: { managed: true, strategy: "quadlet", autoUpdate: false },
    },
  });
}
