// src/network.ts - Network isolation for Arcturus stacks

import { execFileSync } from "node:child_process";

const STACK_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const RUNTIME_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

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

  constructor(engine = "podman", portalContainer = "portal-nginx") {
    if (engine !== "docker" && engine !== "podman" && engine !== "podman-remote") {
      throw new Error(`Unsupported container engine: ${engine}`);
    }
    this.engine = engine;
    this.assertRuntimeName(portalContainer, "portal container");
    this.portalContainer = portalContainer;
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
      // Non-isolated stacks just stay on internal_routing
      return;
    }

    const networkName = `arcturus-${config.stackName}`;

    // 1. Create dedicated bridge network if it doesn't exist
    if (!this.networkExists(networkName)) {
      this.createNetwork(networkName);
    }

    // 2. Connect portal-nginx to this network so it can route
    if (this.containerExists(this.portalContainer)) {
      this.connectContainer(this.portalContainer, networkName);
    }

    // 3. Connect the stack's primary service container
    const containerName = config.containerName || `${config.stackName}-${config.primaryService}`;
    if (this.containerExists(containerName)) {
      this.connectContainer(containerName, networkName);
    }

    // 4. Connect external networks if specified
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

    // Disconnect portal-nginx
    if (this.containerExists(this.portalContainer)) {
      this.disconnectContainer(this.portalContainer, networkName);
    }

    // Remove network
    if (this.networkExists(networkName)) {
      this.removeNetwork(networkName);
    }
  }

  private networkExists(name: string): boolean {
    try {
      execFileSync(this.engine, ["network", "inspect", name], { stdio: "ignore" });
      return true;
    } catch {
      return false;
    }
  }

  private createNetwork(name: string): void {
    try {
      execFileSync(this.engine, ["network", "create", "--driver", "bridge", name], {
        encoding: "utf-8",
        timeout: 10000,
      });
      console.log(`[Network] Created ${name}`);
    } catch (err) {
      console.error(`[Network] Failed to create ${name}:`, (err as Error).message);
    }
  }

  private removeNetwork(name: string): void {
    try {
      execFileSync(this.engine, ["network", "rm", name], {
        encoding: "utf-8",
        timeout: 10000,
      });
      console.log(`[Network] Removed ${name}`);
    } catch (err) {
      console.error(`[Network] Failed to remove ${name}:`, (err as Error).message);
    }
  }

  private containerExists(name: string): boolean {
    try {
      execFileSync(this.engine, ["container", "inspect", name], { stdio: "ignore" });
      return true;
    } catch {
      return false;
    }
  }

  private connectContainer(container: string, network: string): void {
    try {
      execFileSync(this.engine, ["network", "connect", network, container], {
        encoding: "utf-8",
        timeout: 10000,
      });
      console.log(`[Network] Connected ${container} to ${network}`);
    } catch (err) {
      // May already be connected
      const msg = (err as Error).message;
      if (!msg.includes("already exists") && !msg.includes("already connected")) {
        console.error(`[Network] Failed to connect ${container} to ${network}:`, msg);
      }
    }
  }

  private disconnectContainer(container: string, network: string): void {
    try {
      execFileSync(this.engine, ["network", "disconnect", network, container], {
        encoding: "utf-8",
        timeout: 10000,
      });
      console.log(`[Network] Disconnected ${container} from ${network}`);
    } catch (err) {
      // May not be connected
      const msg = (err as Error).message;
      if (!msg.includes("not connected")) {
        console.error(`[Network] Failed to disconnect ${container} from ${network}:`, msg);
      }
    }
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
