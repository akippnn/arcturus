// src/bus.ts - Arcturus Message Bus
// Lightweight pub/sub over Unix domain sockets
// Protocol: newline-delimited JSON

import { createServer, type Server, type Socket } from "node:net";
import { unlinkSync } from "node:fs";

export interface BusMessage {
  topic: string;
  payload: any;
  timestamp: number;
  source?: string;
}

export interface BusSubscription {
  socket: Socket;
  topics: Set<string>;
  id: string;
}

export class MessageBus {
  private server: Server;
  private subscriptions = new Map<string, BusSubscription>();
  private socketPath: string;
  private messageCount = 0;

  constructor(socketPath: string) {
    this.socketPath = socketPath;
    this.server = createServer(this.handleConnection.bind(this));
  }

  start(): Promise<void> {
    return new Promise((resolve, reject) => {
      try { unlinkSync(this.socketPath); } catch { /* ignore */ }
      this.server.listen(this.socketPath, () => {
        console.log(`MessageBus listening on ${this.socketPath}`);
        resolve();
      });
      this.server.on("error", reject);
    });
  }

  stop(): Promise<void> {
    return new Promise((resolve) => {
      for (const sub of this.subscriptions.values()) {
        sub.socket.destroy();
      }
      this.subscriptions.clear();
      this.server.close(() => {
        try { unlinkSync(this.socketPath); } catch { /* ignore */ }
        resolve();
      });
    });
  }

  publish(topic: string, payload: any, source?: string): void {
    const message: BusMessage = {
      topic,
      payload,
      timestamp: Date.now(),
      source,
    };

    const json = JSON.stringify(message) + "\n";
    let delivered = 0;

    for (const sub of this.subscriptions.values()) {
      if (sub.topics.has(topic) || sub.topics.has("*")) {
        try {
          sub.socket.write(json);
          delivered++;
        } catch {
          // Socket dead, will be cleaned up on next error
        }
      }
    }

    this.messageCount++;
    if (this.messageCount % 100 === 0) {
      console.log(`[Bus] ${topic} delivered to ${delivered} subscribers`);
    }
  }

  private handleConnection(socket: Socket): void {
    const clientId = `${socket.remoteAddress || "local"}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let buffer = "";

    const sub: BusSubscription = {
      socket,
      topics: new Set(),
      id: clientId,
    };
    this.subscriptions.set(clientId, sub);

    socket.on("data", (data: Buffer) => {
      buffer += data.toString("utf-8");
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 1);
        this.handleMessage(line, sub);
      }
    });

    socket.on("error", () => {
      this.subscriptions.delete(clientId);
    });

    socket.on("close", () => {
      this.subscriptions.delete(clientId);
    });

    // Send welcome
    this.send(socket, { type: "connected", clientId });
  }

  private handleMessage(line: string, sub: BusSubscription): void {
    try {
      const msg = JSON.parse(line);

      if (msg.action === "subscribe" && Array.isArray(msg.topics)) {
        for (const topic of msg.topics) {
          sub.topics.add(topic);
        }
        this.send(sub.socket, { type: "subscribed", topics: Array.from(sub.topics) });
        return;
      }

      if (msg.action === "unsubscribe" && Array.isArray(msg.topics)) {
        for (const topic of msg.topics) {
          sub.topics.delete(topic);
        }
        this.send(sub.socket, { type: "unsubscribed", topics: Array.from(sub.topics) });
        return;
      }

      if (msg.action === "publish" && msg.topic) {
        this.publish(msg.topic, msg.payload, sub.id);
        return;
      }

      this.send(sub.socket, { type: "error", message: "Unknown action" });
    } catch {
      this.send(sub.socket, { type: "error", message: "Invalid JSON" });
    }
  }

  private send(socket: Socket, msg: any): void {
    try {
      socket.write(JSON.stringify(msg) + "\n");
    } catch { /* ignore */ }
  }
}
