import { DurableObject } from "cloudflare:workers";
import type { Env } from "../env";

/**
 * BidRoom — one Durable Object per opportunity (spec Volume II — "FOREMAN").
 * Holds live per-opportunity state: gate status, workflow progress, and the
 * WebSocket connections that stream progress to the operator's screen. Addressed
 * by opportunity ID: `env.BID_ROOM.idFromName(oppId)`.
 */

interface Progress {
  status: string;
  phase: string; // human-readable, e.g. "shredding chunk 12/40"
  requirementCount: number;
  findingCount: number;
  fatalCount: number;
  updatedAt: number;
}

export class BidRoom extends DurableObject<Env> {
  private sockets = new Set<WebSocket>();
  private progress: Progress = {
    status: "intake",
    phase: "idle",
    requirementCount: 0,
    findingCount: 0,
    fatalCount: 0,
    updatedAt: 0,
  };

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    // WebSocket upgrade for live progress.
    if (request.headers.get("Upgrade") === "websocket") {
      const pair = new WebSocketPair();
      const [client, server] = [pair[0], pair[1]];
      this.ctx.acceptWebSocket(server);
      this.sockets.add(server);
      server.send(JSON.stringify({ type: "snapshot", progress: this.progress }));
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname.endsWith("/progress")) {
      return Response.json(this.progress);
    }

    return new Response("not found", { status: 404 });
  }

  /** Called by the workflow to push a progress update to every connected screen. */
  async update(patch: Partial<Progress>): Promise<void> {
    this.progress = { ...this.progress, ...patch, updatedAt: Date.now() };
    const msg = JSON.stringify({ type: "progress", progress: this.progress });
    for (const ws of this.sockets) {
      try {
        ws.send(msg);
      } catch {
        this.sockets.delete(ws);
      }
    }
  }

  async getProgress(): Promise<Progress> {
    return this.progress;
  }

  webSocketClose(ws: WebSocket): void {
    this.sockets.delete(ws);
  }

  webSocketError(ws: WebSocket): void {
    this.sockets.delete(ws);
  }
}
