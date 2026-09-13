import type { ForemanParams } from "./workflows/foreman";
import type { BidRoom } from "./objects/bidroom";

/**
 * Worker environment bindings. Mirrors wrangler.jsonc.
 */
export interface Env {
  // Secrets
  ANTHROPIC_API_KEY: string;

  // Vars
  SHREDDER_MODEL: string;
  GATEKEEPER_MODEL: string;
  MAX_SHRED_CHUNKS: string;

  // Storage
  DB: D1Database;
  DOCS: R2Bucket;
  SOL_INDEX: VectorizeIndex;
  CORPUS_INDEX: VectorizeIndex;
  AI: Ai;

  // Compute
  BID_ROOM: DurableObjectNamespace<BidRoom>;
  FOREMAN: Workflow<ForemanParams>;
}
