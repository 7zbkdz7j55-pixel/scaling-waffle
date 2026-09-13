/**
 * Document chunking for the shred. SHREDDER is stateless between chunks by
 * design (spec: "statelessness prevents drift across a 300-page package"), so
 * chunks must be self-contained and carry their own page anchor.
 *
 * This is a deliberately simple page-aware chunker. A real extractor produces
 * per-page text with page numbers upstream (pdf text layer / OCR); here we
 * model that as an array of pages and pack them into token-bounded chunks.
 */

export interface Page {
  page: number; // 1-indexed page number as printed
  text: string;
}

export interface Chunk {
  index: number; // 0-indexed chunk number, document order
  startPage: number; // page the chunk begins on — the anchor SHREDDER cites
  text: string;
}

// ~4 chars per token is the usual rough English ratio. We target ~3000 tokens
// of text per chunk to leave generous room for the system prompt + tools.
const TARGET_CHARS = 12_000;

/**
 * Pack sequential pages into chunks no larger than TARGET_CHARS. A single page
 * that exceeds the target is split, but page boundaries are preserved wherever
 * possible so the page anchor stays meaningful.
 */
export function chunkPages(pages: Page[]): Chunk[] {
  const chunks: Chunk[] = [];
  let buf = "";
  let bufStartPage = pages[0]?.page ?? 1;

  const flush = () => {
    if (buf.length === 0) return;
    chunks.push({ index: chunks.length, startPage: bufStartPage, text: buf });
    buf = "";
  };

  for (const p of pages) {
    const marker = `\n\n[[PAGE ${p.page}]]\n`;
    if (buf.length + marker.length + p.text.length > TARGET_CHARS && buf.length > 0) {
      flush();
      bufStartPage = p.page;
    }
    if (buf.length === 0) bufStartPage = p.page;
    buf += marker + p.text;

    // Oversized single page: emit in slices, each anchored to the same page.
    while (buf.length > TARGET_CHARS) {
      const slice = buf.slice(0, TARGET_CHARS);
      chunks.push({ index: chunks.length, startPage: bufStartPage, text: slice });
      buf = buf.slice(TARGET_CHARS);
      bufStartPage = p.page;
    }
  }
  flush();
  return chunks;
}
