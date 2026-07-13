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
  containerCli?: "podman" | "docker";
  commandRunner?: (command: string, args: string[]) => void;
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
          console.warn(`[Router] Security violation: stack '${name}' attempted to bind to ${this.config.baseDomain}. Denying.`);
          continue;
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
    for (const stack of stacks) {
      const config = this.generateVhost(stack);
      if (!config.trim()) continue;
      const filename = `generated-${stack.metadata.name}.conf`;
      if (desired.has(filename)) {
        throw new Error(`Duplicate stack name from registry: ${stack.metadata.name}`);
      }
      desired.set(filename, config);
    }

    try {
      for (const [file, config] of desired) {
        this.writeAtomically(join(this.config.vhostsDir, file), config);
      }
      for (const file of previous.keys()) {
        if (!desired.has(file)) {
          rmSync(join(this.config.vhostsDir, file));
        }
      }
      this.testNginx();
      this.reloadNginx();
      this.writeRoutingStatus(stacks);
    } catch (error) {
      this.restoreConfigs(previous);
      let failure = error as Error;
      try {
        this.testNginx();
        this.reloadNginx();
      } catch (restoreError) {
        failure = new Error(
          `nginx apply failed (${failure.message}); configuration restoration failed (${(restoreError as Error).message})`,
        );
      }
      this.writeRoutingStatus(stacks, failure);
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

  private writeRoutingStatus(stacks: Stack[], failure?: Error): void {
    if (!this.config.statusFile) return;
    const services: Record<string, unknown> = {};
    for (const stack of stacks) {
      const routes = Object.entries(stack.spec.services).flatMap(([serviceName, service]) =>
        (service.domains || []).map(domain => ({
          domain,
          upstream: `${service.containerName || `${stack.metadata.name}-${serviceName}`}:${service.port}`,
        })),
      );
      if (routes.length === 0) continue;
      const filename = `generated-${stack.metadata.name}.conf`;
      const config = existsSync(join(this.config.vhostsDir, filename))
        ? readFileSync(join(this.config.vhostsDir, filename), "utf-8")
        : "";
      services[stack.metadata.name] = {
        status: failure ? "failed" : "published",
        revision: stack.metadata.annotations?.["arcturus.u128.org/revision"] || "legacy",
        deploymentId: stack.metadata.annotations?.["arcturus.u128.org/deployment-id"] || null,
        domains: routes.map(route => route.domain),
        upstreams: routes.map(route => route.upstream),
        configDigest: config
          ? `sha256:${createHash("sha256").update(config).digest("hex")}`
          : null,
        appliedAt: new Date().toISOString(),
        ...(failure ? { error: { code: "nginx_apply_failed", message: this.redactError(failure.message) } } : {}),
      };
    }
    this.writeAtomically(this.config.statusFile, JSON.stringify({ version: 1, services }, null, 2) + "\n");
  }

  private testNginx(): void {
    this.runContainerCommand(["exec", this.config.nginxContainer, "nginx", "-t"]);
  }

  private reloadNginx(): void {
    this.runContainerCommand(["exec", this.config.nginxContainer, "nginx", "-s", "reload"]);
  }

  private runContainerCommand(args: string[]): void {
    const command = this.config.containerCli || "podman";
    if (this.config.commandRunner) {
      this.config.commandRunner(command, args);
      return;
    }
    execFileSync(command, args, { encoding: "utf-8", timeout: 10000 });
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
