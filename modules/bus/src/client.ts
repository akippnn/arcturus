// src/client.ts - Bus client for other modules

import { createConnection, type Socket } from "node:net";

export interface BusClientOptions {
  socketPath: string;
  clientName: string;
}

export class BusClient {
  private socket: Socket | null = null;
  private socketPath: string;
  private clientName: string;
  private handlers = new Map<string, ((payload: any) => void)[]>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private buffer = "";

  constructor(opts: BusClientOptions) {
    this.socketPath = opts.socketPath;
    this.clientName = opts.clientName;
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      let connected = false;
      const timeout = setTimeout(() => {
        this.socket?.destroy();
        reject(new Error("Bus connection timeout"));
      }, 3000);

      this.socket = createConnection(this.socketPath);

      this.socket.on("connect", () => {
        connected = true;
        clearTimeout(timeout);
        console.log(`[${this.clientName}] Connected to bus`);
        resolve();
      });

      this.socket.on("data", (data: Buffer) => {
        this.buffer += data.toString("utf-8");
        let idx;
        while ((idx = this.buffer.indexOf("\n")) >= 0) {
          const line = this.buffer.slice(0, idx);
          this.buffer = this.buffer.slice(idx + 1);
          this.handleMessage(line);
        }
      });

      this.socket.on("error", (err) => {
        clearTimeout(timeout);
        if (!connected) {
          reject(err);
          return;
        }
        console.error(`[${this.clientName}] Bus error:`, err.message);
        this.scheduleReconnect();
      });

      this.socket.on("close", () => {
        if (!connected) return;
        console.log(`[${this.clientName}] Disconnected from bus`);
        this.scheduleReconnect();
      });
    });
  }

  subscribe(topics: string[]): void {
    this.send({ action: "subscribe", topics });
  }

  unsubscribe(topics: string[]): void {
    this.send({ action: "unsubscribe", topics });
  }

  publish(topic: string, payload: any): void {
    this.send({ action: "publish", topic, payload });
  }

  on(topic: string, handler: (payload: any) => void): () => void {
    const list = this.handlers.get(topic) || [];
    list.push(handler);
    this.handlers.set(topic, list);
    return () => {
      const idx = list.indexOf(handler);
      if (idx >= 0) list.splice(idx, 1);
    };
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket?.destroy();
    this.socket = null;
  }

  private send(msg: any): void {
    try {
      this.socket?.write(JSON.stringify(msg) + "\n");
    } catch { /* ignore */ }
  }

  private handleMessage(line: string): void {
    try {
      const msg = JSON.parse(line);
      if (msg.topic) {
        const handlers = this.handlers.get(msg.topic) || [];
        for (const h of handlers) {
          try { h(msg.payload); } catch { /* ignore */ }
        }
      }
    } catch { /* ignore invalid messages */ }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      console.log(`[${this.clientName}] Reconnecting...`);
      this.connect().catch(() => {
        this.scheduleReconnect();
      });
    }, 5000);
  }
}
