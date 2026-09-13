/**
 * Deterministic responsiveness checks (spec Volume II — "Push every check you
 * can out of the model"). The reason is not token cost: a deterministic result
 * needs no human verification, and verification time is the scarcest thing in
 * the business. These run in ordinary code and produce findings identical in
 * shape to GATEKEEPER's, tagged source='deterministic'.
 */

export type Severity = "FATAL" | "MAJOR" | "MINOR";

export interface DeterministicFinding {
  requirement_id: string | null;
  severity: Severity;
  category: string;
  source_page: number | null;
  finding: string;
  remedy: string;
  owner: "CONTRACTOR" | "BROKER" | "SURETY" | "ATTORNEY" | "BIDDESK";
  blocking: boolean;
}

/** What the agency demands, derived from SHREDDER requirements + SCOUT metadata. */
export interface Demands {
  requiredForms: { requirement_id: string; name: string; page: number }[];
  issuedAddendaCount: number;
  dueAtLocal: string | null; // ISO 8601 as stated
  dueTimezone: string | null; // IANA zone or 'UNSTATED'
  minInsuranceLimits: { requirement_id: string; coverage: string; amountUsd: number; page: number }[];
  references: { requirement_id: string; minCount: number; recencyMonths: number | null; page: number } | null;
}

/** What the contractor has actually assembled. */
export interface Package {
  providedFormTitles: string[];
  acknowledgedAddendaCount: number;
  carriedInsurance: { coverage: string; amountUsd: number }[];
  providedReferences: { completedAt: string }[]; // ISO dates
}

/** Normalize a form/coverage title for tolerant matching. */
function norm(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

export function runDeterministicChecks(
  demands: Demands,
  pkg: Package,
  now: number = Date.now(),
): DeterministicFinding[] {
  const findings: DeterministicFinding[] = [];

  // 1. Deadline: unstated time zone is itself a MAJOR risk; a past deadline is FATAL.
  if (!demands.dueAtLocal) {
    findings.push({
      requirement_id: null,
      severity: "MAJOR",
      category: "DEADLINE",
      source_page: null,
      finding: "No submission deadline could be extracted from the package.",
      remedy: "Confirm the due date/time with the procurement officer before proceeding.",
      owner: "CONTRACTOR",
      blocking: false,
    });
  } else if (demands.dueTimezone === "UNSTATED" || !demands.dueTimezone) {
    findings.push({
      requirement_id: null,
      severity: "MAJOR",
      category: "DEADLINE",
      source_page: null,
      finding: `Deadline ${demands.dueAtLocal} has no stated time zone.`,
      remedy: "Confirm the agency's time zone in writing; do not assume local time.",
      owner: "CONTRACTOR",
      blocking: false,
    });
  } else {
    const due = Date.parse(demands.dueAtLocal);
    if (!Number.isNaN(due) && due <= now) {
      findings.push({
        requirement_id: null,
        severity: "FATAL",
        category: "DEADLINE",
        source_page: null,
        finding: `The submission deadline (${demands.dueAtLocal} ${demands.dueTimezone}) has already passed.`,
        remedy: "This solicitation can no longer be bid. Confirm no extension addendum was issued.",
        owner: "CONTRACTOR",
        blocking: true,
      });
    }
  }

  // 2. Addenda: every issued addendum must be acknowledged.
  if (pkg.acknowledgedAddendaCount < demands.issuedAddendaCount) {
    const missing = demands.issuedAddendaCount - pkg.acknowledgedAddendaCount;
    findings.push({
      requirement_id: null,
      severity: "FATAL",
      category: "ADDENDA",
      source_page: null,
      finding: `${missing} of ${demands.issuedAddendaCount} issued addenda are not acknowledged.`,
      remedy: "Acknowledge every issued addendum on the required acknowledgement form.",
      owner: "CONTRACTOR",
      blocking: true,
    });
  }

  // 3. Required forms present, by exact-title match (tolerant of whitespace/case).
  const provided = new Set(pkg.providedFormTitles.map(norm));
  for (const f of demands.requiredForms) {
    if (!provided.has(norm(f.name))) {
      findings.push({
        requirement_id: f.requirement_id,
        severity: "FATAL",
        category: "FORM",
        source_page: f.page,
        finding: `Required form "${f.name}" is not present in the assembled package.`,
        remedy: `Complete and include "${f.name}".`,
        owner: "CONTRACTOR",
        blocking: true,
      });
    }
  }

  // 4. Insurance: each required limit must be met by a carried coverage.
  for (const req of demands.minInsuranceLimits) {
    const carried = pkg.carriedInsurance.find((c) => norm(c.coverage) === norm(req.coverage));
    if (!carried) {
      findings.push({
        requirement_id: req.requirement_id,
        severity: "MAJOR",
        category: "INSURANCE",
        source_page: req.page,
        finding: `No certificate on file for required coverage "${req.coverage}".`,
        remedy: `Obtain a certificate for "${req.coverage}" of at least $${req.amountUsd.toLocaleString()}.`,
        owner: "BROKER",
        blocking: false,
      });
    } else if (carried.amountUsd < req.amountUsd) {
      findings.push({
        requirement_id: req.requirement_id,
        severity: "FATAL",
        category: "INSURANCE",
        source_page: req.page,
        finding: `${req.coverage} limit $${carried.amountUsd.toLocaleString()} is below the required $${req.amountUsd.toLocaleString()}.`,
        remedy: `Increase ${req.coverage} to at least $${req.amountUsd.toLocaleString()} and re-issue the certificate.`,
        owner: "BROKER",
        blocking: true,
      });
    }
  }

  // 5. References: count and recency window.
  if (demands.references) {
    const r = demands.references;
    if (pkg.providedReferences.length < r.minCount) {
      findings.push({
        requirement_id: r.requirement_id,
        severity: "MAJOR",
        category: "REFERENCES",
        source_page: r.page,
        finding: `Only ${pkg.providedReferences.length} references provided; ${r.minCount} required.`,
        remedy: `Add ${r.minCount - pkg.providedReferences.length} more qualifying reference(s).`,
        owner: "CONTRACTOR",
        blocking: false,
      });
    }
    if (r.recencyMonths != null) {
      const cutoff = now - r.recencyMonths * 30 * 24 * 60 * 60 * 1000;
      const stale = pkg.providedReferences.filter((ref) => {
        const t = Date.parse(ref.completedAt);
        return !Number.isNaN(t) && t < cutoff;
      }).length;
      if (stale > 0) {
        findings.push({
          requirement_id: r.requirement_id,
          severity: "MINOR",
          category: "REFERENCES",
          source_page: r.page,
          finding: `${stale} reference(s) fall outside the ${r.recencyMonths}-month recency window.`,
          remedy: "Replace stale references with projects completed inside the window.",
          owner: "CONTRACTOR",
          blocking: false,
        });
      }
    }
  }

  return findings;
}
