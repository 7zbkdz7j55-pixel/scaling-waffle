import type { Env } from "./env";
import { ulid, sha256Hex } from "./lib/ids";
import { getOpportunity, recordGateEvent } from "./lib/db";

// Re-export the Workflow and Durable Object classes so wrangler can bind them.
export { Foreman } from "./workflows/foreman";
export { BidRoom } from "./objects/bidroom";

/**
 * BidDesk Worker API. Static frontend (Netlify) calls these endpoints; the
 * Worker owns intake, orchestration kickoff, and read models. The system never
 * submits a bid — Gate 3 is the human's, always.
 */
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = request.method;

    try {
      // --- health ---
      if (path === "/" || path === "/health") {
        return json({ ok: true, service: "biddesk", version: "0.1.0" });
      }

      // --- tenants ---
      if (path === "/tenants" && method === "POST") {
        const body = await request.json<{ name: string; jurisdiction?: string }>();
        if (!body?.name) return bad("name is required");
        const id = ulid();
        await env.DB.prepare(
          `INSERT INTO tenants (id, name, jurisdiction, created_at) VALUES (?,?,?,?)`,
        )
          .bind(id, body.name, body.jurisdiction ?? null, Date.now())
          .run();
        return json({ id }, 201);
      }

      // --- opportunities ---
      if (path === "/opportunities" && method === "POST") {
        const b = await request.json<{
          tenant_id: string;
          title?: string;
          solicitation_number?: string;
          agency?: string;
          jurisdiction?: string;
          due_at_local?: string;
          due_timezone?: string;
        }>();
        if (!b?.tenant_id) return bad("tenant_id is required");
        const id = ulid();
        const now = Date.now();
        await env.DB.prepare(
          `INSERT INTO opportunities
             (id, tenant_id, title, solicitation_number, agency, jurisdiction,
              due_at_local, due_timezone, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?, 'intake', ?, ?)`,
        )
          .bind(
            id,
            b.tenant_id,
            b.title ?? null,
            b.solicitation_number ?? null,
            b.agency ?? null,
            b.jurisdiction ?? null,
            b.due_at_local ?? null,
            b.due_timezone ?? null,
            now,
            now,
          )
          .run();
        return json({ id }, 201);
      }

      const oppMatch = path.match(/^\/opportunities\/([^/]+)(\/.*)?$/);
      if (oppMatch) {
        const oppId = oppMatch[1];
        const sub = oppMatch[2] ?? "";

        // Upload a document into the package.
        if (sub === "/documents" && method === "POST") {
          const filename = url.searchParams.get("filename") ?? "document";
          const isAddendum = url.searchParams.get("addendum") === "1";
          const bytes = await request.arrayBuffer();
          if (bytes.byteLength === 0) return bad("empty document body");
          const docId = ulid();
          const sha = await sha256Hex(bytes);
          const r2Key = `${oppId}/${docId}/${filename}`;
          await env.DOCS.put(r2Key, bytes);
          await env.DB.prepare(
            `INSERT INTO documents (id, opp_id, filename, r2_key, sha256, is_addendum, created_at)
             VALUES (?,?,?,?,?,?,?)`,
          )
            .bind(docId, oppId, filename, r2Key, sha, isAddendum ? 1 : 0, Date.now())
            .run();
          return json({ id: docId, sha256: sha, r2_key: r2Key }, 201);
        }

        // Kick off the FOREMAN workflow (shred → checks → audit → Gate 1).
        if (sub === "/shred" && method === "POST") {
          const opp = await getOpportunity(env.DB, oppId);
          if (!opp) return notFound("opportunity");
          const tenant = await env.DB.prepare(`SELECT name FROM tenants WHERE id = ?`)
            .bind(opp.tenant_id)
            .first<{ name: string }>();
          const instance = await env.FOREMAN.create({
            params: { oppId, tenantName: tenant?.name ?? "(tenant)" },
          });
          return json({ workflow_id: instance.id, status: "started" }, 202);
        }

        // Read model: opportunity + counts.
        if (sub === "" && method === "GET") {
          const opp = await getOpportunity(env.DB, oppId);
          if (!opp) return notFound("opportunity");
          const counts = await env.DB.prepare(
            `SELECT
               (SELECT COUNT(*) FROM requirements WHERE opp_id = ?1) AS requirements,
               (SELECT COUNT(*) FROM findings WHERE opp_id = ?1) AS findings,
               (SELECT COUNT(*) FROM findings WHERE opp_id = ?1 AND severity = 'FATAL') AS fatal,
               (SELECT COUNT(*) FROM ambiguities WHERE opp_id = ?1) AS ambiguities`,
          )
            .bind(oppId)
            .first();
          return json({ opportunity: opp, counts });
        }

        if (sub === "/requirements" && method === "GET") {
          const res = await env.DB.prepare(
            `SELECT id, doc_id, doc_sha256, page, section_label, source_text, modality,
                    req_type, obligation, points, deliverable_name
             FROM requirements WHERE opp_id = ? ORDER BY seq`,
          )
            .bind(oppId)
            .all();
          return json({ requirements: res.results ?? [] });
        }

        if (sub === "/findings" && method === "GET") {
          const res = await env.DB.prepare(
            `SELECT id, requirement_id, source, severity, category, source_page,
                    finding, remedy, owner, blocking, resolved
             FROM findings WHERE opp_id = ?
             ORDER BY CASE severity WHEN 'FATAL' THEN 0 WHEN 'MAJOR' THEN 1 ELSE 2 END`,
          )
            .bind(oppId)
            .all();
          return json({ findings: res.results ?? [] });
        }

        // Live progress: forward to the opportunity's BidRoom (incl. WS upgrade).
        if (sub === "/progress") {
          const stub = env.BID_ROOM.get(env.BID_ROOM.idFromName(oppId));
          return stub.fetch(request);
        }

        // Record a human gate decision (audit trail).
        const gateMatch = sub.match(/^\/gates\/(GATE[0-3])$/);
        if (gateMatch && method === "POST") {
          const b = await request.json<{ decision: string; actor: string; note?: string }>();
          if (!b?.decision || !b?.actor) return bad("decision and actor are required");
          await recordGateEvent(env.DB, oppId, gateMatch[1], b.decision, b.actor, b.note ?? null);
          return json({ ok: true });
        }
      }

      return notFound("route");
    } catch (err) {
      const message = err instanceof Error ? err.message : "internal error";
      return json({ error: message }, 500);
    }
  },
} satisfies ExportedHandler<Env>;

// ---- response helpers ----
function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}
function bad(message: string): Response {
  return json({ error: message }, 400);
}
function notFound(what: string): Response {
  return json({ error: `${what} not found` }, 404);
}
