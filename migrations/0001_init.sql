-- BidDesk relational spine (spec Volume II — "D1: relational spine").
--
-- Design decision that everything hangs on: every requirement carries full
-- provenance (document hash, page, char offset, verbatim source text), and
-- nothing renders into a deliverable without a traceable chain back to it.
-- The v1 slice ships SHREDDER + GATEKEEPER, so the evidence/section/pricing
-- tables that later agents need are intentionally NOT created yet — add them
-- when a paying client is blocked on ARCHIVIST/DRAFTER.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Tenants: one contractor company. Five users per tenant is the design point.
-- --------------------------------------------------------------------------
CREATE TABLE tenants (
  id            TEXT PRIMARY KEY,           -- ULID
  name          TEXT NOT NULL,
  jurisdiction  TEXT,                        -- primary operating jurisdiction, e.g. 'US-GA'
  created_at    INTEGER NOT NULL             -- epoch ms
);

-- --------------------------------------------------------------------------
-- Opportunities: one solicitation the tenant is (considering) responding to.
-- --------------------------------------------------------------------------
CREATE TABLE opportunities (
  id                  TEXT PRIMARY KEY,       -- ULID; also the BidRoom DO name and sol:{opp_id} vector namespace
  tenant_id           TEXT NOT NULL REFERENCES tenants(id),
  title               TEXT,
  solicitation_number TEXT,
  agency              TEXT,
  jurisdiction        TEXT,
  due_at_local        TEXT,                   -- ISO 8601 as stated in the document
  due_timezone        TEXT,                   -- IANA zone, or 'UNSTATED'
  -- Workflow lifecycle. Gates are the human-in-the-loop checkpoints.
  status              TEXT NOT NULL DEFAULT 'intake'
                        CHECK (status IN ('intake','shredding','shredded','auditing',
                                          'audited','gate1_pending','ready','archived','error')),
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);
CREATE INDEX idx_opp_tenant ON opportunities(tenant_id);

-- --------------------------------------------------------------------------
-- Documents: every file in the package. The hash is the provenance anchor.
-- --------------------------------------------------------------------------
CREATE TABLE documents (
  id            TEXT PRIMARY KEY,             -- ULID
  opp_id        TEXT NOT NULL REFERENCES opportunities(id),
  filename      TEXT NOT NULL,
  r2_key        TEXT NOT NULL,                -- object key in the DOCS bucket
  sha256        TEXT NOT NULL,                -- content hash — cited by every requirement
  page_count    INTEGER,
  is_addendum   INTEGER NOT NULL DEFAULT 0,   -- boolean
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_doc_opp ON documents(opp_id);

-- --------------------------------------------------------------------------
-- Requirements: SHREDDER output. One atomic obligation per row, with provenance.
-- --------------------------------------------------------------------------
CREATE TABLE requirements (
  id                TEXT PRIMARY KEY,         -- ULID
  opp_id            TEXT NOT NULL REFERENCES opportunities(id),
  doc_id            TEXT NOT NULL REFERENCES documents(id),
  doc_sha256        TEXT NOT NULL,            -- denormalized provenance: hash the row was cut from
  page              INTEGER NOT NULL,
  char_offset       INTEGER NOT NULL,         -- offset within the shredded chunk
  section_label     TEXT,                     -- as printed, e.g. '3.2.1' or 'Attachment C'
  source_text       TEXT NOT NULL,            -- VERBATIM, unmodified
  modality          TEXT NOT NULL CHECK (modality IN
                        ('SHALL','MUST','WILL','SHOULD','MAY','IMPLIED')),
  req_type          TEXT NOT NULL CHECK (req_type IN
                        ('SUBMITTAL','ADMINISTRATIVE','PERFORMANCE','EVALUATION','FLOWDOWN')),
  obligation        TEXT NOT NULL,            -- restated as a single imperative
  points            REAL,                     -- stated point value/weight, if any
  deliverable_name  TEXT,                     -- exact printed name of a demanded form/doc
  seq               INTEGER NOT NULL,         -- document order within the opportunity
  created_at        INTEGER NOT NULL
);
CREATE INDEX idx_req_opp ON requirements(opp_id, seq);

-- --------------------------------------------------------------------------
-- Ambiguities: SHREDDER flags, not resolutions. Become buyer questions.
-- --------------------------------------------------------------------------
CREATE TABLE ambiguities (
  id                TEXT PRIMARY KEY,
  opp_id            TEXT NOT NULL REFERENCES opportunities(id),
  doc_id            TEXT NOT NULL REFERENCES documents(id),
  page              INTEGER NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN
                        ('CONTRADICTION','MISSING_REFERENCE','DUAL_READING','ILLEGIBLE')),
  source_text       TEXT NOT NULL,
  readings_json     TEXT,                     -- JSON array of candidate readings
  suggested_question TEXT NOT NULL,
  created_at        INTEGER NOT NULL
);
CREATE INDEX idx_amb_opp ON ambiguities(opp_id);

-- --------------------------------------------------------------------------
-- Findings: GATEKEEPER output. FATAL findings hard-block Gate 3 in the UI.
-- Findings from deterministic code checks are also stored here (source column).
-- --------------------------------------------------------------------------
CREATE TABLE findings (
  id            TEXT PRIMARY KEY,
  opp_id        TEXT NOT NULL REFERENCES opportunities(id),
  requirement_id TEXT REFERENCES requirements(id),  -- nullable: deterministic checks may have none
  source        TEXT NOT NULL DEFAULT 'gatekeeper'
                  CHECK (source IN ('gatekeeper','deterministic')),
  severity      TEXT NOT NULL CHECK (severity IN ('FATAL','MAJOR','MINOR')),
  category      TEXT NOT NULL,                -- FORM, SIGNATURE, DEADLINE, ... (see agent tool)
  source_page   INTEGER,
  finding       TEXT NOT NULL,               -- what is wrong, one sentence
  remedy        TEXT,                        -- the exact action that fixes it
  owner         TEXT NOT NULL DEFAULT 'CONTRACTOR'
                  CHECK (owner IN ('CONTRACTOR','BROKER','SURETY','ATTORNEY','BIDDESK')),
  blocking      INTEGER NOT NULL DEFAULT 0,   -- boolean
  resolved      INTEGER NOT NULL DEFAULT 0,   -- boolean; set when the human clears/overrides
  override_note TEXT,                         -- typed acknowledgement when a FATAL is overridden
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_find_opp ON findings(opp_id, severity);

-- --------------------------------------------------------------------------
-- Gate events: the audit trail of every human decision at a checkpoint.
-- --------------------------------------------------------------------------
CREATE TABLE gate_events (
  id            TEXT PRIMARY KEY,
  opp_id        TEXT NOT NULL REFERENCES opportunities(id),
  gate          TEXT NOT NULL CHECK (gate IN ('GATE0','GATE1','GATE2','GATE3')),
  decision      TEXT NOT NULL,               -- e.g. 'pursue','approved','override'
  actor         TEXT NOT NULL,               -- user id / email
  note          TEXT,
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_gate_opp ON gate_events(opp_id);

-- --------------------------------------------------------------------------
-- Jurisdiction rulebook: learned quirks per agency. The compounding moat.
-- Cross-tenant and anonymized (no tenant_id by design).
-- --------------------------------------------------------------------------
CREATE TABLE jurisdiction_rules (
  id            TEXT PRIMARY KEY,
  agency        TEXT NOT NULL,
  jurisdiction  TEXT NOT NULL,
  topic         TEXT NOT NULL CHECK (topic IN
                  ('portal','forms','insurance','bonding','preference','delivery','history')),
  rule          TEXT NOT NULL,               -- the learned quirk, plain text
  source_opp_id TEXT,                         -- where we learned it (nullable, may be pruned)
  created_at    INTEGER NOT NULL
);
CREATE INDEX idx_rules_agency ON jurisdiction_rules(agency, jurisdiction, topic);

-- --------------------------------------------------------------------------
-- Model call ledger: token + cost per opportunity so unit margin is known
-- from day one (spec Volume II — "you know your unit margin per bid").
-- --------------------------------------------------------------------------
CREATE TABLE model_calls (
  id                  TEXT PRIMARY KEY,
  opp_id              TEXT REFERENCES opportunities(id),
  agent               TEXT NOT NULL,          -- 'shredder' | 'gatekeeper'
  model               TEXT NOT NULL,
  input_tokens        INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
  est_cost_usd        REAL NOT NULL DEFAULT 0,
  created_at          INTEGER NOT NULL
);
CREATE INDEX idx_calls_opp ON model_calls(opp_id);
