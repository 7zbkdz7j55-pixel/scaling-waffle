import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import type { Env } from "../env";
import { chunkPages, type Page } from "../lib/chunk";
import { shredChunk } from "../agents/shredder";
import { runGatekeeper } from "../agents/gatekeeper";
import { runDeterministicChecks, type Demands, type Package } from "../deterministic/checks";
import {
  getOpportunity,
  setOpportunityStatus,
  persistShredCalls,
  listRequirements,
  persistGatekeeperFindings,
  persistDeterministicFindings,
  getJurisdictionRules,
} from "../lib/db";
import type { PreambleVars } from "../agents/preamble";

/**
 * FOREMAN — the orchestration layer (spec Volume II).
 *
 * A Cloudflare Workflow, not a loop in a Worker: shredding a 300-page package
 * is 40+ model calls over several minutes, well past any request timeout.
 * Workflows give durable execution, per-step retry with backoff, and state that
 * survives a failed step — so a timeout on chunk 180 doesn't lose chunks 1–179.
 *
 * v1 pipeline (SHREDDER + GATEKEEPER only):
 *   intake → shred every chunk of every doc → deterministic checks →
 *   GATEKEEPER responsiveness audit → Gate 1 pending (human review).
 */

export interface ForemanParams {
  oppId: string;
  tenantName: string;
}

interface DocManifest {
  docId: string;
  sha256: string;
  r2Key: string;
  chunkCount: number;
}

export class Foreman extends WorkflowEntrypoint<Env, ForemanParams> {
  async run(event: WorkflowEvent<ForemanParams>, step: WorkflowStep): Promise<void> {
    const { oppId, tenantName } = event.payload;
    const env = this.env;
    const room = env.BID_ROOM.get(env.BID_ROOM.idFromName(oppId));

    // ---- Load the opportunity and its documents (one durable read). ----
    const setup = await step.do("load-opportunity", async () => {
      const opp = await getOpportunity(env.DB, oppId);
      if (!opp) throw new Error(`opportunity ${oppId} not found`);
      const docs = await env.DB.prepare(
        `SELECT id, r2_key, sha256 FROM documents WHERE opp_id = ? ORDER BY is_addendum, created_at`,
      )
        .bind(oppId)
        .all<{ id: string; r2_key: string; sha256: string }>();
      const vars: PreambleVars = {
        tenantName,
        oppId,
        solicitationTitle: opp.title ?? "(untitled)",
        agency: opp.agency ?? "(unknown agency)",
        jurisdiction: opp.jurisdiction ?? "(unknown)",
      };
      return { opp, docs: docs.results ?? [], vars };
    });

    await setOpportunityStatus(env.DB, oppId, "shredding");
    await room.update({ status: "shredding", phase: "starting shred" });

    // ---- Build per-document chunk manifests. chunkPages is deterministic, so
    // shred steps can re-derive the same chunk by index without storing text in
    // workflow state. ----
    const manifests: DocManifest[] = [];
    for (const doc of setup.docs) {
      const m = await step.do(`chunk-doc:${doc.id}`, async () => {
        const pages = await loadPages(env, doc.r2_key);
        return {
          docId: doc.id,
          sha256: doc.sha256,
          r2Key: doc.r2_key,
          chunkCount: chunkPages(pages).length,
        } satisfies DocManifest;
      });
      manifests.push(m);
    }

    const totalChunks = manifests.reduce((n, m) => n + m.chunkCount, 0);
    const maxChunks = Number(env.MAX_SHRED_CHUNKS) || 400;
    if (totalChunks > maxChunks) {
      await setOpportunityStatus(env.DB, oppId, "error");
      await room.update({ status: "error", phase: `package too large: ${totalChunks} chunks` });
      throw new Error(`package exceeds MAX_SHRED_CHUNKS (${totalChunks} > ${maxChunks})`);
    }

    // ---- Shred each chunk in its own durable step. seq is kept monotonic by
    // packing chunk index into it (chunkBase); persistShredCalls assigns the
    // fine-grained order within a chunk. ----
    let done = 0;
    for (const m of manifests) {
      for (let idx = 0; idx < m.chunkCount; idx++) {
        await step.do(`shred:${m.docId}:${idx}`, { retries: { limit: 3, delay: "10 seconds", backoff: "exponential" } }, async () => {
          const pages = await loadPages(env, m.r2Key);
          const chunk = chunkPages(pages)[idx];
          if (!chunk) return { emitted: 0 };
          const calls = await shredChunk({ env, oppId, docId: m.docId, chunk, vars: setup.vars });
          // seq base leaves 10k slots per chunk — plenty for atomic requirements.
          const seqBase = (manifests.indexOf(m) * 1000 + idx) * 10_000;
          await persistShredCalls(env.DB, oppId, m.docId, m.sha256, calls, seqBase);
          return { emitted: calls.length };
        });
        done++;
        await room.update({ phase: `shredding chunk ${done}/${totalChunks}` });
      }
    }

    const reqCount = await step.do("count-requirements", async () => {
      const r = await env.DB.prepare(`SELECT COUNT(*) AS n FROM requirements WHERE opp_id = ?`)
        .bind(oppId)
        .first<{ n: number }>();
      return r?.n ?? 0;
    });
    await setOpportunityStatus(env.DB, oppId, "shredded");
    await room.update({ status: "shredded", phase: "shred complete", requirementCount: reqCount });

    // ---- Deterministic checks (run in code, need no human verification). At
    // the audit stage only solicitation-level facts are known — deadline sanity
    // and time zone. Form-presence / insurance / references / addenda-ack checks
    // run later against the assembled package (Gate 3), with full Demands+Package. ----
    await step.do("deterministic-checks", async () => {
      const demands: Demands = {
        requiredForms: [],
        issuedAddendaCount: 0,
        dueAtLocal: setup.opp.due_at_local,
        dueTimezone: setup.opp.due_timezone,
        minInsuranceLimits: [],
        references: null,
      };
      const emptyPackage: Package = {
        providedFormTitles: [],
        acknowledgedAddendaCount: 0,
        carriedInsurance: [],
        providedReferences: [],
      };
      const findings = runDeterministicChecks(demands, emptyPackage);
      await persistDeterministicFindings(env.DB, oppId, findings);
      return { findings: findings.length };
    });

    // ---- GATEKEEPER responsiveness audit over the extracted requirements. ----
    await setOpportunityStatus(env.DB, oppId, "auditing");
    await room.update({ status: "auditing", phase: "responsiveness audit" });

    const auditSummary = await step.do("gatekeeper-audit", { retries: { limit: 2, delay: "15 seconds", backoff: "exponential" } }, async () => {
      const requirements = await listRequirements(env.DB, oppId);
      const rules = await getJurisdictionRules(env.DB, setup.opp.agency, setup.opp.jurisdiction);
      const calls = await runGatekeeper({
        env,
        oppId,
        vars: setup.vars,
        requirements,
        jurisdictionRules: rules,
      });
      const validIds = new Set(requirements.map((r) => r.id));
      const { inserted, rejected } = await persistGatekeeperFindings(env.DB, oppId, calls, validIds);
      return { inserted, rejected };
    });

    const fatal = await step.do("count-fatal", async () => {
      const r = await env.DB.prepare(
        `SELECT COUNT(*) AS n FROM findings WHERE opp_id = ? AND severity = 'FATAL'`,
      )
        .bind(oppId)
        .first<{ n: number }>();
      return r?.n ?? 0;
    });

    await setOpportunityStatus(env.DB, oppId, "gate1_pending");
    await room.update({
      status: "gate1_pending",
      phase: "awaiting human review (Gate 1)",
      findingCount: auditSummary.inserted,
      fatalCount: fatal,
    });
  }
}

/**
 * Load a document's pages. An upstream extractor is expected to write a
 * `${key}.pages.json` sidecar (an array of {page, text}); absent that, the raw
 * object is decoded as UTF-8 text and treated as a single page. Real PDF text
 * extraction / OCR is out of scope for the v1 scaffold.
 */
async function loadPages(env: Env, r2Key: string): Promise<Page[]> {
  const sidecar = await env.DOCS.get(`${r2Key}.pages.json`);
  if (sidecar) {
    const parsed = (await sidecar.json()) as Page[];
    if (Array.isArray(parsed) && parsed.length > 0) return parsed;
  }
  const raw = await env.DOCS.get(r2Key);
  if (!raw) throw new Error(`document object missing in R2: ${r2Key}`);
  return [{ page: 1, text: await raw.text() }];
}
