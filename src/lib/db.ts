import type { EmittedCall } from "./anthropic";
import type { DeterministicFinding } from "../deterministic/checks";
import type { RequirementView } from "../agents/gatekeeper";
import { ulid } from "./ids";

/**
 * D1 persistence. Kept as plain functions over the binding rather than an ORM —
 * the schema is small and the queries are few.
 */

export interface OpportunityRow {
  id: string;
  tenant_id: string;
  title: string | null;
  agency: string | null;
  jurisdiction: string | null;
  due_at_local: string | null;
  due_timezone: string | null;
  status: string;
}

export async function getOpportunity(db: D1Database, id: string): Promise<OpportunityRow | null> {
  return db
    .prepare(
      `SELECT id, tenant_id, title, agency, jurisdiction, due_at_local, due_timezone, status
       FROM opportunities WHERE id = ?`,
    )
    .bind(id)
    .first<OpportunityRow>();
}

export async function setOpportunityStatus(db: D1Database, id: string, status: string): Promise<void> {
  await db
    .prepare(`UPDATE opportunities SET status = ?, updated_at = ? WHERE id = ?`)
    .bind(status, Date.now(), id)
    .run();
}

/**
 * Persist SHREDDER output for one chunk. `seqStart` is the running document-order
 * counter so requirements across chunks stay in order. Returns the next seq.
 */
export async function persistShredCalls(
  db: D1Database,
  oppId: string,
  docId: string,
  docSha256: string,
  calls: EmittedCall[],
  seqStart: number,
): Promise<number> {
  let seq = seqStart;
  const stmts: D1PreparedStatement[] = [];
  const now = Date.now();

  for (const call of calls) {
    if (call.name === "emit_requirement") {
      const i = call.input;
      stmts.push(
        db
          .prepare(
            `INSERT INTO requirements
               (id, opp_id, doc_id, doc_sha256, page, char_offset, section_label,
                source_text, modality, req_type, obligation, points, deliverable_name, seq, created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
          )
          .bind(
            ulid(),
            oppId,
            docId,
            docSha256,
            num(i.page),
            num(i.char_offset),
            str(i.section_label),
            str(i.source_text) ?? "",
            str(i.modality) ?? "IMPLIED",
            str(i.req_type) ?? "PERFORMANCE",
            str(i.obligation) ?? "",
            i.points == null ? null : Number(i.points),
            str(i.deliverable_name),
            seq++,
            now,
          ),
      );
    } else if (call.name === "flag_ambiguity") {
      const i = call.input;
      stmts.push(
        db
          .prepare(
            `INSERT INTO ambiguities
               (id, opp_id, doc_id, page, kind, source_text, readings_json, suggested_question, created_at)
             VALUES (?,?,?,?,?,?,?,?,?)`,
          )
          .bind(
            ulid(),
            oppId,
            docId,
            num(i.page),
            str(i.kind) ?? "DUAL_READING",
            str(i.source_text) ?? "",
            i.readings ? JSON.stringify(i.readings) : null,
            str(i.suggested_question) ?? "",
            now,
          ),
      );
    }
  }

  if (stmts.length > 0) await db.batch(stmts);
  return seq;
}

export async function listRequirements(db: D1Database, oppId: string): Promise<RequirementView[]> {
  const res = await db
    .prepare(
      `SELECT id, page, section_label, modality, req_type, obligation, source_text, deliverable_name
       FROM requirements WHERE opp_id = ? ORDER BY seq`,
    )
    .bind(oppId)
    .all<RequirementView>();
  return res.results ?? [];
}

/** Persist GATEKEEPER findings. Only accepts findings whose requirement_id is real. */
export async function persistGatekeeperFindings(
  db: D1Database,
  oppId: string,
  calls: EmittedCall[],
  validRequirementIds: Set<string>,
): Promise<{ inserted: number; rejected: number }> {
  const stmts: D1PreparedStatement[] = [];
  const now = Date.now();
  let rejected = 0;

  for (const call of calls) {
    if (call.name !== "emit_finding") continue;
    const i = call.input;
    const reqId = str(i.requirement_id);
    // Spec: "A finding without a citation is not a finding." Enforce it.
    if (!reqId || !validRequirementIds.has(reqId)) {
      rejected++;
      continue;
    }
    stmts.push(
      db
        .prepare(
          `INSERT INTO findings
             (id, opp_id, requirement_id, source, severity, category, source_page,
              finding, remedy, owner, blocking, created_at)
           VALUES (?,?,?,'gatekeeper',?,?,?,?,?,?,?,?)`,
        )
        .bind(
          ulid(),
          oppId,
          reqId,
          str(i.severity) ?? "MAJOR",
          str(i.category) ?? "OTHER",
          num(i.source_page),
          str(i.finding) ?? "",
          str(i.remedy),
          str(i.owner) ?? "CONTRACTOR",
          i.blocking ? 1 : 0,
          now,
        ),
    );
  }

  if (stmts.length > 0) await db.batch(stmts);
  return { inserted: stmts.length, rejected };
}

export async function persistDeterministicFindings(
  db: D1Database,
  oppId: string,
  findings: DeterministicFinding[],
): Promise<void> {
  if (findings.length === 0) return;
  const now = Date.now();
  const stmts = findings.map((f) =>
    db
      .prepare(
        `INSERT INTO findings
           (id, opp_id, requirement_id, source, severity, category, source_page,
            finding, remedy, owner, blocking, created_at)
         VALUES (?,?,?,'deterministic',?,?,?,?,?,?,?,?)`,
      )
      .bind(
        ulid(),
        oppId,
        f.requirement_id,
        f.severity,
        f.category,
        f.source_page,
        f.finding,
        f.remedy,
        f.owner,
        f.blocking ? 1 : 0,
        now,
      ),
  );
  await db.batch(stmts);
}

export async function getJurisdictionRules(
  db: D1Database,
  agency: string | null,
  jurisdiction: string | null,
): Promise<string[]> {
  if (!agency) return [];
  const res = await db
    .prepare(
      `SELECT rule FROM jurisdiction_rules
       WHERE agency = ? AND (jurisdiction = ? OR ? IS NULL)
       ORDER BY created_at DESC LIMIT 50`,
    )
    .bind(agency, jurisdiction, jurisdiction)
    .all<{ rule: string }>();
  return (res.results ?? []).map((r) => r.rule);
}

export async function recordGateEvent(
  db: D1Database,
  oppId: string,
  gate: string,
  decision: string,
  actor: string,
  note: string | null,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO gate_events (id, opp_id, gate, decision, actor, note, created_at)
       VALUES (?,?,?,?,?,?,?)`,
    )
    .bind(ulid(), oppId, gate, decision, actor, note, Date.now())
    .run();
}

// ---- small coercion helpers so bad model output can't throw on bind() ----
function str(v: unknown): string | null {
  return typeof v === "string" ? v : v == null ? null : String(v);
}
function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}
