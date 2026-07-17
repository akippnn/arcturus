// src/network.ts - Network isolation for Arcturus stacks

import { execFileSync } from "node:child_process";

const STACK_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const RUNTIME_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

type NetworkCommandRunner = (command: string, args: string[]) => string | void;

export interface NetworkConfig {
  stackName: string;
  isolate: boolean;
  external?: string[];
  primaryService: string;
  port: number;
  containerName?: string;
}

export class NetworkManager {
  private engine: string;
  private portalContainer: string;
  private commandRunner?: NetworkCommandRunner;

  constructor(
    engine = "podman",
    portalContainer = "portal-nginx",
    commandRunner?: NetworkCommandRunner,
  ) {
    if (engine !== "docker" && engine !== "podman") {
      throw new Error(`Unsupported container engine: ${engine}`);
    }
    this.engine = engine;
    this.assertRuntimeName(portalContainer, "portal container");
    this.portalContainer = portalContainer;
    this.commandRunner = commandRunner;
  }

  /** Ensure a stack has its isolated network and portal can reach it */
  async ensureStackNetwork(config: NetworkConfig): Promise<void> {
    this.assertStackName(config.stackName);
    this.assertRuntimeName(config.primaryService, "service");
    if (!Number.isInteger(config.port) || config.port < 1 || config.port > 65535) {
      throw new Error(`Invalid service port: ${config.port}`);
    }
    if (config.containerName) {
      this.assertRuntimeName(config.containerName, "container");
    }
    for (const network of config.external || []) {
      this.assertRuntimeName(network, "network");
    }

    if (!config.isolate) {
      return;
    }

    const networkName = `arcturus-${config.stackName}`;

    if (!this.networkExists(networkName)) {
      this.createNetwork(networkName);
    }

    if (this.containerExists(this.portalContainer)) {
      this.connectContainer(this.portalContainer, networkName);
    }

    const containerName = config.containerName || `${config.stackName}-${config.primaryService}`;
    if (this.containerExists(containerName)) {
      this.connectContainer(containerName, networkName);
    }

    for (const extNet of config.external || []) {
      if (this.containerExists(containerName)) {
        this.connectContainer(containerName, extNet);
      }
    }
  }

  /** Remove stack's isolated network and disconnect portal */
  async removeStackNetwork(stackName: string): Promise<void> {
    this.assertStackName(stackName);
    const networkName = `arcturus-${stackName}`;

    if (this.containerExists(this.portalContainer)) {
      this.disconnectContainer(this.portalContainer, networkName);
    }

    if (this.networkExists(networkName)) {
      this.removeNetwork(networkName);
    }
  }

  private run(args: string[]): string {
    if (this.commandRunner) {
      return this.commandRunner(this.engine, args) || "";
    }
    return execFileSync(this.engine, args, {
      encoding: "utf-8",
      timeout: 10000,
      stdio: ["ignore", "pipe", "pipe"],
    });
  }

  private networkExists(name: string): boolean {
    try {
      this.run(["network", "inspect", name]);
      return true;
    } catch {
      return false;
    }
  }

  private createNetwork(name: string): void {
    try {
      this.run(["network", "create", "--driver", "bridge", name]);
      console.log(`[Network] Created ${name}`);
    } catch (error) {
      throw new Error(`Failed to create network ${name}: ${this.errorMessage(error)}`);
    }
  }

  private removeNetwork(name: string): void {
    try {
      this.run(["network", "rm", name]);
      console.log(`[Network] Removed ${name}`);
    } catch (error) {
      throw new Error(`Failed to remove network ${name}: ${this.errorMessage(error)}`);
    }
  }

  private containerExists(name: string): boolean {
    try {
      this.run(["container", "inspect", name]);
      return true;
    } catch {
      return false;
    }
  }

  private containerConnectedToNetwork(container: string, network: string): boolean {
    try {
      const raw = this.run([
        "container",
        "inspect",
        "--format",
        "{{json .NetworkSettings.Networks}}",
        container,
      ]).trim();
      const networks = JSON.parse(raw || "{}") as Record<string, unknown>;
      return Object.prototype.hasOwnProperty.call(networks, network);
    } catch {
      return false;
    }
  }

  private connectContainer(container: string, network: string): void {
    if (this.containerConnectedToNetwork(container, network)) {
      return;
    }

    try {
      this.run(["network", "connect", network, container]);
      console.log(`[Network] Connected ${container} to ${network}`);
    } catch (error) {
      const message = this.errorMessage(error);
      if (message.includes("already exists") || message.includes("already connected")) {
        return;
      }
      throw new Error(`Failed to connect ${container} to ${network}: ${message}`);
    }
  }

  private disconnectContainer(container: string, network: string): void {
    if (!this.containerConnectedToNetwork(container, network)) {
      return;
    }

    try {
      this.run(["network", "disconnect", network, container]);
      console.log(`[Network] Disconnected ${container} from ${network}`);
    } catch (error) {
      const message = this.errorMessage(error);
      if (message.includes("not connected") || message.includes("no such network")) {
        return;
      }
      throw new Error(`Failed to disconnect ${container} from ${network}: ${message}`);
    }
  }

  private errorMessage(error: unknown): string {
    const failure = error as Error & { stderr?: string | Buffer };
    const stderr = typeof failure.stderr === "string"
      ? failure.stderr
      : failure.stderr?.toString("utf-8");
    return (stderr || failure.message || String(error)).trim().slice(0, 512);
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
}
