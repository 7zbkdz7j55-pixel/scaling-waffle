# BidDesk

A multi-agent bid response system for small contractors selling to school
districts, cities, counties, and state agencies (SLED). This repository is the
**v1 slice** of the [build specification](#spec): the two agents that ship
first — **SHREDDER** and **GATEKEEPER** — on the Cloudflare stack, with the
FOREMAN orchestration layer and the provenance-first data model that everything
else depends on.

> **Wedge:** everyone else sells "write proposals faster." BidDesk sells
> something narrower and harder to argue with — *"you will not be thrown out on
> a technicality again."* Responsiveness first, narrative second.

## What's in this repo (and what isn't)

Seven agents is the finished system; two ship first, because the binding
constraint is delivery/review hours, not build hours. Only SHREDDER and
GATEKEEPER are implemented here. ARCHIVIST, DRAFTER, PRICER, REDLINE, and SCOUT
are deliberately **not** built until a paying client is blocked on them.

| Agent | Status | Role |
|---|---|---|
| **SHREDDER** | ✅ v1 | Decomposes the package into atomic, traceable requirements |
| **GATEKEEPER** | ✅ v1 | Responsiveness audit — the checks that get a bid rejected unread |
| ARCHIVIST / DRAFTER / PRICER / REDLINE / SCOUT | ⏳ later | See the spec |

## The design decision everything hangs on

Every requirement row carries **provenance**: document hash (`doc_sha256`), page
number, character offset, and verbatim source text. This is simultaneously the
anti-hallucination mechanism, the defensibility moat, the sales demo (click any
row, see the page it came from), and the legal protection when a past-performance
claim is later disputed. It is built on day one (`migrations/0001_init.sql`)
rather than retrofitted.

Two rules are enforced in code, not left to the model:
- **SHREDDER** quotes `source_text` verbatim and records page + offset per row.
- **GATEKEEPER** findings without a real `requirement_id` citation are rejected
  before they hit the database (`persistGatekeeperFindings`).

## Architecture

```
Worker API (src/index.ts)
  └─ FOREMAN  (Cloudflare Workflow, src/workflows/foreman.ts)
       ├─ per-chunk SHREDDER step  → requirements (D1, with provenance)
       ├─ deterministic checks     → findings (D1)
       └─ GATEKEEPER audit step    → findings (D1) → Gate 1 (human)
  └─ BidRoom  (Durable Object, one per opportunity) — live progress over WS
```

- **FOREMAN is a Workflow, not a loop in a Worker.** Shredding a 300-page
  package is 40+ model calls over several minutes, past any request timeout.
  Each chunk is its own durable step with retry/backoff, so a failure on chunk
  180 doesn't lose chunks 1–179.
- **Model tiering is the margin.** GATEKEEPER runs Opus (judgment); SHREDDER
  runs Sonnet (high-volume classification). Both use adaptive thinking; effort
  is tuned per agent. Model IDs are `vars` in `wrangler.jsonc`, overridable per
  environment.
- **Deterministic checks are pushed out of the model** (`src/deterministic/`).
  A deterministic result needs no human verification, and verification time is
  the scarcest thing in the business. Page counts, deadline arithmetic, addenda
  tallies, numeric insurance-limit comparisons, and form-presence by title match
  run in ordinary code. The model is reserved for prose judgment calls.
- **The model-call ledger** (`model_calls`) logs tokens and estimated cost per
  opportunity so unit margin per bid is visible from day one.

## Layout

```
migrations/0001_init.sql     D1 schema — the relational spine
src/index.ts                 Worker HTTP API + class re-exports
src/workflows/foreman.ts     FOREMAN — durable orchestration
src/objects/bidroom.ts       BidRoom — per-opportunity live state (DO)
src/agents/preamble.ts       Shared preamble (the three override rules)
src/agents/shredder.ts       SHREDDER prompt + tools + runner
src/agents/gatekeeper.ts     GATEKEEPER prompt + tools + runner
src/deterministic/checks.ts  Checks that belong in code, not a prompt
src/lib/anthropic.ts         Claude agentic loop + cost ledger
src/lib/db.ts                D1 persistence
src/lib/chunk.ts             Page-aware, deterministic chunker
src/lib/ids.ts               ULID + SHA-256 provenance hash
```

## Setup

```bash
npm install

# 1. Create the resources named in wrangler.jsonc
npx wrangler d1 create biddesk           # paste the database_id into wrangler.jsonc
npx wrangler r2 bucket create biddesk-docs
npx wrangler vectorize create biddesk-solicitations --dimensions=1024 --metric=cosine
npx wrangler vectorize create biddesk-corpus --dimensions=1024 --metric=cosine

# 2. Apply the schema
npm run migrate:remote                   # or migrate:local for the local dev DB

# 3. Provide the Claude API key
npx wrangler secret put ANTHROPIC_API_KEY

# 4. Run / deploy
npm run dev
npm run deploy
```

`npm run typecheck` runs `tsc --noEmit`.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/tenants` | Create a contractor tenant |
| `POST` | `/opportunities` | Create an opportunity (solicitation) |
| `POST` | `/opportunities/:id/documents?filename=…&addendum=0/1` | Upload a package document (raw body) |
| `POST` | `/opportunities/:id/shred` | Kick off FOREMAN (shred → checks → audit → Gate 1) |
| `GET`  | `/opportunities/:id` | Opportunity + requirement/finding counts |
| `GET`  | `/opportunities/:id/requirements` | Extracted requirements with provenance |
| `GET`  | `/opportunities/:id/findings` | Responsiveness findings, FATAL first |
| `GET`  | `/opportunities/:id/progress` | Live progress (WebSocket upgrade supported) |
| `POST` | `/opportunities/:id/gates/:GATE0-3` | Record a human gate decision (audit trail) |

### Document intake

FOREMAN loads each document's pages from a `${r2_key}.pages.json` sidecar (an
array of `{ page, text }`) if present, otherwise decodes the raw object as UTF-8
text as a single page. Real PDF text extraction / OCR is upstream of this
scaffold and intentionally out of scope for v1.

## Human-in-the-loop gates

The system **never submits a bid.** Gate 3 is the human's — a bid is a binding
offer, and the person legally bound must transmit it. v1 reaches **Gate 1**
(human reviews the requirement matrix and GATEKEEPER findings; every FATAL
finding must be resolved or explicitly overridden in writing). Gate decisions
are recorded in `gate_events`.

<a name="spec"></a>
## Spec

The full build specification (all seven agents, evaluation targets, cost model,
go-to-market) lives in the BidDesk build-spec artifact this scaffold was built
from.
