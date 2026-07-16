// src/router.ts - Router Module: generates nginx configs from Registry state

import { writeFileSync, readFileSync, existsSync, mkdirSync, rmSync, readdirSync, renameSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { request } from "node:http";

const STACK_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const RUNTIME_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const DNS_LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const BODY_SIZE_RE = /^[1-9][0-9]{0,8}[kKmMgG]?$/;
// Minimal Stack type for Router (avoid cross-module import)
interface Stack {
  metadata: { name: string; annotations?: Record<string, string> };
  spec: {
    services: Record<string, {
      port: number;
      domains?: string[];
      aliases?: string[];
      maxBodySize?: string;
      websocket?: boolean;
      nginxExtras?: string;
      containerName?: string;
    }>;
    redirects?: Record<string, {
      from: string;
      to: string;
      code?: number;
    }>;
  };
}

export interface RouterConfig {
  vhostsDir: string;
  nginxContainer: string;
  baseDomain: string;
  certDomain: string;
  apexService?: string;
  registrySocket: string;
  statusFile?: string;
  containerCli?: "podman" | "podman-remote" | "docker";
  commandRunner?: (command: string, args: string[]) => string | void;
  pidExists?: (pid: number) => boolean;
  pathExists?: (path: string) => boolean;
}

interface VerificationCheck {
  name: string;
  status: "passed" | "failed";
  message?: string;
}

interface RouteVerification {
  domain: string;
  upstream: string;
  status: "passed" | "failed";
  checks: VerificationCheck[];
}

interface ServiceVerification {
  status: "passed" | "failed";
  routes: RouteVerification[];
  restoration?: VerificationCheck[];
}

export class Router {
  private config: RouterConfig;

  constructor(config: RouterConfig) {
    this.config = config;
    mkdirSync(config.vhostsDir, { recursive: true });
  }

  /** Fetch all stacks from Registry API via Unix socket */
  async fetchStacks(): Promise<Stack[]> {
    return new Promise((resolve, reject) => {
      const req = request({
        socketPath: this.config.registrySocket,
        path: "/stacks",
        method: "GET",
        headers: { Host: "arcturus-registry" },
      }, (res) => {
        let data = "";
        res.on("data", chunk => data += chunk);
        res.on("end", () => {
          if (res.statusCode === 200) {
            try {
              const stacks = JSON.parse(data);
              if (!Array.isArray(stacks)) {
                throw new Error("Registry response must be an array");
              }
              resolve(stacks);
            } catch (e) {
              reject(e);
            }
          } else {
            reject(new Error(`Registry returned ${res.statusCode}: ${data}`));
          }
        });
      });
      req.on("error", reject);
      req.end();
    });
  }

  /** Generate nginx vhost config for a stack */
  generateVhost(stack: Stack): string {
    const { certDomain } = this.config;
    const name = stack.metadata.name;
    const { spec } = stack;
    const lines: string[] = [];
    this.assertStackName(name);
    this.assertDomain(certDomain);
    this.assertDomain(this.config.baseDomain);

    // Redirect server blocks
    if (spec.redirects) {
      for (const redirect of Object.values(spec.redirects)) {
        this.assertDomain(redirect.from);
        this.assertRedirectTarget(redirect.to);
        if (redirect.code && ![301, 302, 307, 308].includes(redirect.code)) {
          throw new Error(`Unsupported redirect code: ${redirect.code}`);
        }
        lines.push(`server {`);
        lines.push(`    listen 443 ssl;`);
        lines.push(`    server_name ${redirect.from};`);
        lines.push(`    ssl_certificate /etc/letsencrypt/live/${certDomain}/fullchain.pem;`);
        lines.push(`    ssl_certificate_key /etc/letsencrypt/live/${certDomain}/privkey.pem;`);
        lines.push(`    return ${redirect.code || 301} ${redirect.to};`);
        lines.push(`}`);
        lines.push(``);
      }
    }

    for (const [serviceName, service] of Object.entries(spec.services)) {
      this.assertRuntimeName(serviceName, "service");
      if (!Number.isInteger(service.port) || service.port < 1 || service.port > 65535) {
        throw new Error(`Invalid port for ${name}/${serviceName}`);
      }
      if (service.maxBodySize && !BODY_SIZE_RE.test(service.maxBodySize)) {
        throw new Error(`Invalid maxBodySize for ${name}/${serviceName}`);
      }
      if (service.nginxExtras && (service.nginxExtras.length > 4096 || /[{}]/.test(service.nginxExtras))) {
        throw new Error(`Unsafe nginxExtras for ${name}/${serviceName}`);
      }

      const targetDomains = new Set<string>();
      if (service.domains) {
        for (const d of service.domains) {
          this.assertDomain(d);
          targetDomains.add(d);
        }
      }
      if (service.aliases) {
        for (const alias of service.aliases) {
          if (!DNS_LABEL_RE.test(alias)) {
            throw new Error(`Invalid alias for ${name}/${serviceName}: ${alias}`);
          }
          targetDomains.add(`${alias}.${this.config.baseDomain}`);
        }
      }

      if (targetDomains.size === 0) continue;

      const upstreamHost = service.containerName || `${name}-${serviceName}`;
      this.assertRuntimeName(upstreamHost, "upstream");

      for (const domain of targetDomains) {
        if (domain === this.config.baseDomain && name !== this.config.apexService) {
          throw new Error(
            `apex route denied for ${name}: ARCTURUS_APEX_SERVICE must authorize this service`,
          );
        }
        lines.push(`server {`);
        lines.push(`    listen 443 ssl;`);
        lines.push(`    server_name ${domain};`);
        lines.push(`    ssl_certificate /etc/letsencrypt/live/${certDomain}/fullchain.pem;`);
        lines.push(`    ssl_certificate_key /etc/letsencrypt/live/${certDomain}/privkey.pem;`);
        lines.push(`    client_max_body_size ${service.maxBodySize || "1G"};`);
        lines.push(`    proxy_intercept_errors on;`);
        lines.push(`    error_page 502 503 504 @offline;`);
        lines.push(`    location @offline {`);
        lines.push(`        root /var/www/main;`);
        lines.push(`        add_header X-Arcturus-Status "offline" always;`);
        lines.push(`        add_header 'Access-Control-Allow-Origin' '*' always;`);
        lines.push(`        rewrite ^ /index.html break;`);
        lines.push(`    }`);
        lines.push(`    location / {`);
        lines.push(`        set $upstream_target "http://${upstreamHost}:${service.port}";`);
        lines.push(`        proxy_pass $upstream_target;`);
        lines.push(`        proxy_set_header Host $host;`);
        lines.push(`        proxy_set_header X-Real-IP $remote_addr;`);
        lines.push(`        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`);
        lines.push(`        proxy_set_header X-Forwarded-Proto $scheme;`);
        lines.push(`        proxy_set_header X-Forwarded-Host $host;`);
        lines.push(`        proxy_set_header X-Forwarded-Port $server_port;`);
        lines.push(`        proxy_http_version 1.1;`);
        if (service.websocket) {
          lines.push(`        proxy_set_header Upgrade $http_upgrade;`);
          lines.push(`        proxy_set_header Connection "upgrade";`);
        } else {
          lines.push(`        proxy_set_header Connection "";`);
        }
        lines.push(`        proxy_buffering off; proxy_request_buffering off;`);
        lines.push(`        proxy_read_timeout 86400;`);
        lines.push(`        proxy_send_timeout 86400;`);
        lines.push(`    }`);
        if (service.nginxExtras) {
          lines.push(`    ${service.nginxExtras}`);
        }
        lines.push(`}`);
        lines.push(``);
      }
    }

    return lines.join("\n");
  }

  /** Write all vhost configs and reload nginx */
  async apply(stacks: Stack[]): Promise<void> {
    const previous = new Map<string, string>();
    for (const file of this.listGeneratedConfigs()) {
      previous.set(file, readFileSync(join(this.config.vhostsDir, file), "utf-8"));
    }

    const desired = new Map<string, string>();
    const verification = this.initialVerification(stacks);

    try {
      for (const stack of stacks) {
        const config = this.generateVhost(stack);
        if (!config.trim()) continue;
        const filename = `generated-${stack.metadata.name}.conf`;
        if (desired.has(filename)) {
          throw new Error(`Duplicate stack name from registry: ${stack.metadata.name}`);
        }
        desired.set(filename, config);
      }
      this.verifyRuntimeTargets(verification);
      for (const [file, config] of desired) {
        this.writeAtomically(join(this.config.vhostsDir, file), config);
      }
      for (const file of previous.keys()) {
        if (!desired.has(file)) {
          rmSync(join(this.config.vhostsDir, file));
        }
      }
      this.verifyGeneratedVhosts(verification);
      this.runVerifiedNginxStep(verification, "nginx-test", () => this.testNginx());
      this.runVerifiedNginxStep(verification, "nginx-reload", () => this.reloadNginx());
      this.writeRoutingStatus(stacks, undefined, verification);
    } catch (error) {
      this.markUnverifiedRoutesFailed(verification, error as Error);
      this.restoreConfigs(previous);
      let failure = error as Error;
      const restoration: VerificationCheck[] = [];
      try {
        this.testNginx();
        restoration.push({ name: "nginx-test", status: "passed" });
        this.reloadNginx();
        restoration.push({ name: "nginx-reload", status: "passed" });
      } catch (restoreError) {
        restoration.push({
          name: "nginx-restoration",
          status: "failed",
          message: this.redactError((restoreError as Error).message),
        });
        failure = new Error(
          `nginx apply failed (${failure.message}); configuration restoration failed (${(restoreError as Error).message})`,
        );
      }
      for (const service of Object.values(verification)) {
        service.restoration = restoration;
      }
      this.writeRoutingStatus(stacks, failure, verification);
      throw failure;
    }
  }

  /** Remove a single stack's vhost */
  async removeVhost(stackName: string): Promise<void> {
    const filename = `generated-${stackName}.conf`;
    const path = join(this.config.vhostsDir, filename);
    this.assertStackName(stackName);
    if (!existsSync(path)) {
      return;
    }
    let previous: string | undefined;
    try {
      previous = readFileSync(path, "utf-8");
      rmSync(path);
      this.testNginx();
      this.reloadNginx();
    } catch (error) {
      if (previous !== undefined) {
        writeFileSync(path, previous, "utf-8");
      }
      throw error;
    }
  }

  private listGeneratedConfigs(): string[] {
    try {
      return readdirSync(this.config.vhostsDir)
        .filter(f => f.startsWith("generated-") && f.endsWith(".conf"));
    } catch {
      return [];
    }
  }

  private restoreConfigs(previous: Map<string, string>): void {
    for (const file of this.listGeneratedConfigs()) {
      rmSync(join(this.config.vhostsDir, file));
    }
    for (const [file, config] of previous) {
      this.writeAtomically(join(this.config.vhostsDir, file), config);
    }
  }

  private writeAtomically(path: string, contents: string): void {
    mkdirSync(dirname(path), { recursive: true });
    const temporary = `${path}.${randomUUID()}.tmp`;
    writeFileSync(temporary, contents, { encoding: "utf-8", mode: 0o600 });
    renameSync(temporary, path);
  }

  private writeRoutingStatus(
    stacks: Stack[],
    failure?: Error,
    verification: Record<string, ServiceVerification> = {},
  ): void {
    if (!this.config.statusFile) return;
    const services: Record<string, unknown> = {};
    for (const stack of stacks) {
      const routes = this.routesForStack(stack);
      if (routes.length === 0) continue;
      const filename = `generated-${stack.metadata.name}.conf`;
      const configPath = join(this.config.vhostsDir, filename);
      const config = this.configExists(configPath)
        ? readFileSync(join(this.config.vhostsDir, filename), "utf-8")
        : "";
      const serviceVerification = verification[stack.metadata.name] || {
        status: "failed",
        routes: routes.map(route => ({
          ...route,
          status: "failed" as const,
          checks: [{ name: "verification", status: "failed" as const, message: "not run" }],
        })),
      };
      const published = !failure && serviceVerification.status === "passed" && Boolean(config);
      services[stack.metadata.name] = {
        status: published ? "published" : "failed",
        revision: stack.metadata.annotations?.["arcturus.u128.org/revision"] || "legacy",
        deploymentId: stack.metadata.annotations?.["arcturus.u128.org/deployment-id"] || null,
        domains: routes.map(route => route.domain),
        upstreams: routes.map(route => route.upstream),
        configDigest: config
          ? `sha256:${createHash("sha256").update(config).digest("hex")}`
          : null,
        appliedAt: new Date().toISOString(),
        verification: serviceVerification,
        ...(!published ? {
          error: {
            code: failure ? "route_publication_failed" : "route_verification_failed",
            message: this.redactError(failure?.message || "route verification failed"),
          },
        } : {}),
      };
    }
    this.writeAtomically(this.config.statusFile, JSON.stringify({ version: 1, services }, null, 2) + "\n");
  }

  private routesForStack(stack: Stack): Array<{ domain: string; upstream: string }> {
    return Object.entries(stack.spec.services).flatMap(([serviceName, service]) => {
      const upstream = `${service.containerName || `${stack.metadata.name}-${serviceName}`}:${service.port}`;
      return [
        ...(service.domains || []).map(domain => ({ domain, upstream })),
        ...(service.aliases || []).map(alias => ({
          domain: `${alias}.${this.config.baseDomain}`,
          upstream,
        })),
      ];
    });
  }

  private initialVerification(stacks: Stack[]): Record<string, ServiceVerification> {
    return Object.fromEntries(
      stacks
        .map(stack => [
          stack.metadata.name,
          {
            status: "passed" as const,
            routes: this.routesForStack(stack).map(route => ({
              ...route,
              status: "passed" as const,
              checks: [],
            })),
          },
        ])
        .filter(([, value]) => (value as ServiceVerification).routes.length > 0),
    );
  }

  private verifyRuntimeTargets(verification: Record<string, ServiceVerification>): void {
    const cached = new Map<string, { status: "passed" | "failed"; checks: VerificationCheck[] }>();
    for (const service of Object.values(verification)) {
      for (const route of service.routes) {
        const [host, rawPort] = route.upstream.split(":");
        const port = Number(rawPort);
        const cacheKey = `${host}:${port}`;
        const previous = cached.get(cacheKey);
        if (previous) {
          route.status = previous.status;
          route.checks.push(...previous.checks.map(check => ({ ...check })));
          if (previous.status === "failed") {
            service.status = "failed";
          }
          continue;
        }
        try {
          this.runContainerCommand(["exec", this.config.nginxContainer, "getent", "hosts", host]);
          route.checks.push({ name: "portal-dns", status: "passed" });
          const state = this.resolveContainerState(host);
          if (!state.Running || state.Status !== "running") {
            throw new Error(`container ${host} is not running`);
          }
          route.checks.push({ name: "container", status: "passed" });
          if (!Number.isInteger(state.Pid) || state.Pid <= 0 || !this.pidExists(state.Pid)) {
            throw new Error(`container ${host} has no live host PID`);
          }
          route.checks.push({ name: "pid", status: "passed" });
          this.runContainerCommand([
            "exec",
            this.config.nginxContainer,
            "nc",
            "-z",
            "-w",
            "3",
            host,
            String(port),
          ]);
          route.checks.push({ name: "upstream-port", status: "passed" });
          cached.set(cacheKey, {
            status: "passed",
            checks: route.checks.map(check => ({ ...check })),
          });
        } catch (error) {
          route.status = "failed";
          service.status = "failed";
          route.checks.push({
            name: "runtime",
            status: "failed",
            message: this.redactError((error as Error).message),
          });
          cached.set(cacheKey, {
            status: "failed",
            checks: route.checks.map(check => ({ ...check })),
          });
        }
      }
    }
    const failed = Object.values(verification)
      .flatMap(service => service.routes)
      .filter(route => route.status === "failed");
    if (failed.length > 0) {
      throw new Error(`route runtime verification failed: ${failed.map(route => route.upstream).join(", ")}`);
    }
  }

  private resolveContainerState(host: string): { Running: boolean; Status: string; Pid: number } {
    try {
      return JSON.parse(
        this.runContainerCommand(["inspect", "--format", "{{json .State}}", host]),
      );
    } catch {
      const ids = this.runContainerCommand(["ps", "-q"]).split(/\s+/).filter(Boolean);
      for (const id of ids) {
        const inspected = JSON.parse(
          this.runContainerCommand(["inspect", "--format", "{{json .}}", id]),
        );
        const aliases = Object.values(inspected.NetworkSettings?.Networks || {})
          .flatMap((network: any) => network.Aliases || []);
        if (aliases.includes(host)) {
          return inspected.State;
        }
      }
      throw new Error(`no live container owns upstream name ${host}`);
    }
  }

  private verifyGeneratedVhosts(verification: Record<string, ServiceVerification>): void {
    for (const [name, service] of Object.entries(verification)) {
      const path = join(this.config.vhostsDir, `generated-${name}.conf`);
      const exists = this.configExists(path);
      for (const route of service.routes) {
        route.checks.push({
          name: "vhost",
          status: exists ? "passed" : "failed",
          ...(!exists ? { message: `generated vhost is missing for ${name}` } : {}),
        });
        if (!exists) {
          route.status = "failed";
          service.status = "failed";
        }
      }
    }
    if (Object.values(verification).some(service => service.status === "failed")) {
      throw new Error("generated vhost verification failed");
    }
  }

  private runVerifiedNginxStep(
    verification: Record<string, ServiceVerification>,
    name: string,
    action: () => void,
  ): void {
    try {
      action();
      for (const service of Object.values(verification)) {
        for (const route of service.routes) {
          route.checks.push({ name, status: "passed" });
        }
      }
    } catch (error) {
      for (const service of Object.values(verification)) {
        service.status = "failed";
        for (const route of service.routes) {
          route.status = "failed";
          route.checks.push({
            name,
            status: "failed",
            message: this.redactError((error as Error).message),
          });
        }
      }
      throw error;
    }
  }

  private markUnverifiedRoutesFailed(
    verification: Record<string, ServiceVerification>,
    error: Error,
  ): void {
    for (const service of Object.values(verification)) {
      for (const route of service.routes) {
        if (route.checks.length === 0) {
          route.status = "failed";
          service.status = "failed";
          route.checks.push({
            name: "configuration",
            status: "failed",
            message: this.redactError(error.message),
          });
        }
      }
    }
  }

  private testNginx(): void {
    this.runContainerCommand(["exec", this.config.nginxContainer, "nginx", "-t"]);
  }

  private reloadNginx(): void {
    this.runContainerCommand(["exec", this.config.nginxContainer, "nginx", "-s", "reload"]);
  }

  private runContainerCommand(args: string[]): string {
    const command = this.config.containerCli || "podman";
    if (this.config.commandRunner) {
      return this.config.commandRunner(command, args) || "";
    }
    return execFileSync(command, args, { encoding: "utf-8", timeout: 10000 });
  }

  private pidExists(pid: number): boolean {
    return this.config.pidExists ? this.config.pidExists(pid) : existsSync(`/proc/${pid}`);
  }

  private configExists(path: string): boolean {
    return this.config.pathExists ? this.config.pathExists(path) : existsSync(path);
  }

  private redactError(message: string): string {
    return message
      .replace(/(authorization|token|password|secret|credential)(\s*[:=]\s*)\S+/gi, "$1$2[REDACTED]")
      .slice(0, 512);
  }

  private assertStackName(name: string): void {
    if (!STACK_NAME_RE.test(name)) {
      throw new Error(`Invalid stack name: ${name}`);
    }
  }

  private assertRuntimeName(name: string, kind: string): void {
    if (!RUNTIME_NAME_RE.test(name)) {
      throw new Error(`Invalid ${kind} name: ${name}`);
    }
  }

  private assertDomain(domain: string): void {
    if (domain.length > 253 || domain.endsWith(".") || !domain.split(".").every(label => DNS_LABEL_RE.test(label))) {
      throw new Error(`Invalid domain: ${domain}`);
    }
  }

  private assertRedirectTarget(target: string): void {
    if (/[\r\n;]/.test(target)) {
      throw new Error(`Invalid redirect target: ${target}`);
    }
    const parsed = new URL(target);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error(`Invalid redirect protocol: ${parsed.protocol}`);
    }
  }
}
