import type Anthropic from "@anthropic-ai/sdk";
import type { Env } from "../env";
import { preamble, type PreambleVars } from "./preamble";
import { runAgent, type EmittedCall } from "../lib/anthropic";

/**
 * GATEKEEPER — responsiveness audit (spec Volume IV).
 * Opus. The agent that justifies the price: it audits only the things that get
 * a bid rejected before anyone reads it.
 *
 * Note: the deterministic, checkable items (page counts, deadline arithmetic,
 * addenda counts, numeric limit comparisons, form-presence by title match) are
 * handled in ../deterministic/checks.ts and NOT asked of the model — a
 * deterministic result needs no human verification. GATEKEEPER is reserved for
 * the judgment calls (signature/notary language, ambiguous format instructions,
 * additional-insured wording).
 */

const GATEKEEPER_ROLE = `ROLE: Determine whether this bid would be rejected as non-responsive.

You are not a writing coach. You do not care whether the narrative is
persuasive. You care about exactly one question: if this package were
opened by the procurement officer tomorrow morning, is there any
reason it would be set aside unread?

Most public bids are lost to clerical failure, not weak content. You
are the control for that failure mode.

AUDIT EVERY ONE OF THESE, EVERY TIME:
1. Required forms — present, correct version, correct edition date
2. Signatures — wet signature versus electronic, who is authorized
3. Notarization — which documents, and whether the seal is required
4. Bid bond — correct form, correct percentage, surety listed
5. Insurance certificates — each limit met, additional-insured
   wording present, waiver of subrogation if demanded
6. Addenda — every issued addendum acknowledged, on the right form
7. Page limits — per volume and per section, and what counts toward
   them (are appendices excluded? dividers? the cover?)
8. Formatting — font family, minimum point size, margins, line
   spacing, single or double sided
9. File format — PDF version, searchable or not, combined or separate
   files, maximum file size
10. File naming convention, if specified
11. Number of copies, hard copy versus electronic, and where each goes
12. Submission channel — portal, email, physical delivery address,
    and whether a physical delivery requires a specific label
13. Deadline in the AGENCY's time zone, converted to the
    contractor's local time
14. Required registrations active as of the due date
15. Licensure current in the issuing jurisdiction
16. References — count, recency window, format, contact fields
17. Anything in the jurisdiction rulebook for this agency

SEVERITY:
- FATAL: the bid is non-responsive as it stands. Say so plainly, in
  one sentence, and state exactly what must change.
- MAJOR: scored points will be lost, or rejection is plausible.
- MINOR: cosmetic or best-practice.

For every finding, cite the requirement ID and the source page. A
finding without a citation is not a finding — do not emit it.

If you are uncertain whether something is FATAL, call it FATAL. A
false alarm costs the contractor ten minutes. A miss costs them the
contract.`;

export const GATEKEEPER_TOOLS: Anthropic.Tool[] = [
  {
    name: "check_artifact",
    description: "Inspect a submitted or planned artifact against a requirement.",
    input_schema: {
      type: "object",
      properties: {
        artifact_id: { type: "string" },
        requirement_id: { type: "string" },
        aspects: {
          type: "array",
          items: {
            type: "string",
            enum: ["presence", "version", "signature", "notary", "format", "page_count", "naming", "limits", "dates"],
          },
        },
      },
      required: ["artifact_id", "requirement_id", "aspects"],
    },
  },
  {
    name: "query_jurisdiction_rules",
    description: "Retrieve learned quirks for this agency or jurisdiction from the rulebook.",
    input_schema: {
      type: "object",
      properties: {
        agency: { type: "string" },
        jurisdiction: { type: "string" },
        topic: {
          type: "string",
          enum: ["portal", "forms", "insurance", "bonding", "preference", "delivery", "history"],
        },
      },
      required: ["agency", "jurisdiction"],
    },
  },
  {
    name: "emit_finding",
    description: "Emit one responsiveness finding. FATAL findings hard-block submission.",
    input_schema: {
      type: "object",
      properties: {
        severity: { type: "string", enum: ["FATAL", "MAJOR", "MINOR"] },
        category: {
          type: "string",
          enum: [
            "FORM", "SIGNATURE", "NOTARY", "BOND", "INSURANCE", "ADDENDA",
            "PAGE_LIMIT", "FORMAT", "FILE", "COPIES", "CHANNEL", "DEADLINE",
            "REGISTRATION", "LICENSE", "REFERENCES", "OTHER",
          ],
        },
        requirement_id: { type: "string" },
        source_page: { type: "integer" },
        finding: {
          type: "string",
          maxLength: 240,
          description: "What is wrong. One sentence. No hedging.",
        },
        remedy: { type: "string", maxLength: 240, description: "The exact action that fixes it." },
        owner: { type: "string", enum: ["CONTRACTOR", "BROKER", "SURETY", "ATTORNEY", "BIDDESK"] },
        blocking: { type: "boolean" },
      },
      required: ["severity", "category", "requirement_id", "source_page", "finding", "remedy", "owner", "blocking"],
    },
  },
];

/** A requirement row as GATEKEEPER needs to see it — id + provenance + text. */
export interface RequirementView {
  id: string;
  page: number;
  section_label: string | null;
  modality: string;
  req_type: string;
  obligation: string;
  source_text: string;
  deliverable_name: string | null;
}

export interface GatekeeperInput {
  env: Env;
  oppId: string;
  vars: PreambleVars;
  requirements: RequirementView[];
  /** Learned quirks for this agency, injected so the model needn't tool-call for them. */
  jurisdictionRules: string[];
}

/**
 * Run the responsiveness audit over the extracted requirements. Returns the raw
 * emitted findings; the query_jurisdiction_rules tool is answered inline from
 * the rules we already loaded (a single DB read up front beats a round-trip).
 */
export async function runGatekeeper(input: GatekeeperInput): Promise<EmittedCall[]> {
  const system = `${preamble(input.vars)}\n\n${GATEKEEPER_ROLE}`;

  const reqTable = input.requirements
    .map(
      (r) =>
        `[${r.id}] p${r.page}${r.section_label ? ` §${r.section_label}` : ""} ` +
        `(${r.modality}/${r.req_type}${r.deliverable_name ? `, form: "${r.deliverable_name}"` : ""})\n` +
        `    obligation: ${r.obligation}\n` +
        `    source: ${JSON.stringify(r.source_text)}`,
    )
    .join("\n\n");

  const rulesBlock =
    input.jurisdictionRules.length > 0
      ? input.jurisdictionRules.map((r) => `- ${r}`).join("\n")
      : "(none recorded yet for this agency)";

  const userContent = `Jurisdiction rulebook for this agency:
${rulesBlock}

Extracted requirements (id, provenance, and verbatim source). Audit these for
responsiveness. Deterministic checks (page counts, deadline arithmetic, addenda
tallies, numeric limit comparisons, form-presence by exact title) have already
run in code — do not re-derive them. Focus on the judgment calls: signature and
notarization language, ambiguous format instructions, additional-insured and
waiver-of-subrogation wording, and anything the agency wrote as prose instead of
a rule.

Every emit_finding MUST carry a real requirement_id from the list below.

--- REQUIREMENTS ---
${reqTable}
--- END REQUIREMENTS ---`;

  const { calls } = await runAgent({
    env: input.env,
    oppId: input.oppId,
    agent: "gatekeeper",
    model: input.env.GATEKEEPER_MODEL,
    system,
    tools: GATEKEEPER_TOOLS,
    userContent,
    effort: "high", // this is the judgment agent; do not skimp
    maxTurns: 6,
  });
  return calls;
}
