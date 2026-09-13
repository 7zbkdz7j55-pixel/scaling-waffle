import type Anthropic from "@anthropic-ai/sdk";
import type { Env } from "../env";
import { preamble, type PreambleVars } from "./preamble";
import { runAgent, type EmittedCall } from "../lib/anthropic";
import type { Chunk } from "../lib/chunk";

/**
 * SHREDDER — requirement decomposition (spec Volume IV).
 * Sonnet, high volume, chunked map-reduce. No memory between chunks by design.
 */

const SHREDDER_ROLE = `ROLE: Decompose a solicitation into atomic, traceable requirements.

You receive ONE chunk of the package at a time, with its document ID
and starting page. You have no memory of other chunks. Do not
speculate about content outside your chunk.

WHAT IS A REQUIREMENT: any statement that creates an obligation on
the bidder, or that the agency will use to evaluate the bid. This
includes obligations buried in the scope of work, general conditions,
special conditions, attachments, and addenda — not only the
instructions section. Assume requirements are hidden outside the
obvious sections, because they are.

ATOMICITY IS ABSOLUTE. One obligation per emitted requirement. If a
sentence says "Contractor shall provide a safety plan, an OSHA 300
log, and proof of training," that is THREE requirements, not one.
Over-splitting is a minor annoyance. Under-splitting loses bids.

FOR EACH REQUIREMENT:
- Quote the source text verbatim. Do not clean it up, do not fix its
  grammar, do not expand its abbreviations.
- Record page number and character offset within the chunk.
- Classify modality: SHALL, MUST, WILL, SHOULD, MAY, or IMPLIED.
  IMPLIED is for obligations stated without modal verbs ("Bidders
  submit three copies"). Use it; agencies write this way constantly.
- Classify type: SUBMITTAL (something to hand in), ADMINISTRATIVE
  (form, signature, format, deadline), PERFORMANCE (what the work
  must achieve), EVALUATION (how the bid is scored), FLOWDOWN
  (something imposed on subcontractors).
- Note whether it carries a stated point value or weight.

AMBIGUITY: if a requirement is internally contradictory, references a
document not in the package, or could reasonably be read two ways,
call flag_ambiguity. Do NOT resolve it. Ambiguities become written
questions to the buyer, which is a deliverable in itself.

Emit requirements in document order. Emit nothing else.`;

export const SHREDDER_TOOLS: Anthropic.Tool[] = [
  {
    name: "emit_requirement",
    description:
      "Emit one atomic requirement. Call once per obligation, in document order.",
    input_schema: {
      type: "object",
      properties: {
        source_text: { type: "string", description: "Verbatim. Unmodified." },
        doc_id: { type: "string" },
        page: { type: "integer" },
        char_offset: { type: "integer" },
        section_label: {
          type: "string",
          description: "As printed, e.g. '3.2.1' or 'Attachment C'",
        },
        modality: { type: "string", enum: ["SHALL", "MUST", "WILL", "SHOULD", "MAY", "IMPLIED"] },
        req_type: {
          type: "string",
          enum: ["SUBMITTAL", "ADMINISTRATIVE", "PERFORMANCE", "EVALUATION", "FLOWDOWN"],
        },
        obligation: {
          type: "string",
          maxLength: 300,
          description: "The obligation restated as a single imperative. No softening.",
        },
        points: { type: ["number", "null"] },
        deliverable_name: {
          type: ["string", "null"],
          description: "If a named form or document is demanded, its exact printed name",
        },
      },
      required: ["source_text", "doc_id", "page", "char_offset", "modality", "req_type", "obligation"],
    },
  },
  {
    name: "flag_ambiguity",
    description:
      "Flag unresolvable ambiguity for human review and possible buyer question.",
    input_schema: {
      type: "object",
      properties: {
        source_text: { type: "string" },
        page: { type: "integer" },
        kind: {
          type: "string",
          enum: ["CONTRADICTION", "MISSING_REFERENCE", "DUAL_READING", "ILLEGIBLE"],
        },
        readings: { type: "array", items: { type: "string" } },
        suggested_question: {
          type: "string",
          description: "A question the contractor could submit to the buyer, in their words",
        },
      },
      required: ["source_text", "page", "kind", "suggested_question"],
    },
  },
];

export interface ShredChunkInput {
  env: Env;
  oppId: string;
  docId: string;
  chunk: Chunk;
  vars: PreambleVars;
}

/**
 * Shred a single chunk. Returns the raw emitted calls (emit_requirement /
 * flag_ambiguity); persistence and sequencing are the caller's job so a failed
 * chunk in a Workflow doesn't lose the chunks before it.
 */
export async function shredChunk(input: ShredChunkInput): Promise<EmittedCall[]> {
  const system = `${preamble(input.vars)}\n\n${SHREDDER_ROLE}`;
  const userContent = `Document ID: ${input.docId}
Chunk index: ${input.chunk.index}
Starting page: ${input.chunk.startPage}

--- BEGIN CHUNK ---
${input.chunk.text}
--- END CHUNK ---

Decompose this chunk. Character offsets are relative to the first character of
the chunk text above (the "B" of "BEGIN" is not part of it). Emit requirements
in document order; flag ambiguities rather than resolving them.`;

  const { calls } = await runAgent({
    env: input.env,
    oppId: input.oppId,
    agent: "shredder",
    model: input.env.SHREDDER_MODEL,
    system,
    tools: SHREDDER_TOOLS,
    userContent,
    effort: "medium", // classification task; Sonnet handles it well without max effort
    maxTurns: 4,
  });
  return calls;
}
