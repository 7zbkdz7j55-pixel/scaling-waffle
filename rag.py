"""
rag.py — a local knowledge base with semantic search.

Indexes your own documents and retrieves the passages that actually answer a
question. Runs standalone, and backs forge.py's rag_ingest / rag_search /
rag_list tools.

Nothing leaves the machine: embeddings are computed locally and the index is a
couple of files on disk.

Two embedders
-------------
`SentenceTransformerEmbedder` is the real one — sentence-transformers computing
true semantic vectors, so "what does it cost" finds a passage about pricing.
First use downloads the model (~90MB).

`HashingEmbedder` is the fallback, used automatically when sentence-transformers
is not installed or its model cannot be fetched. It is **lexical, not semantic**:
feature-hashed bag-of-words, so it matches shared wording and misses paraphrase.
It keeps the build usable offline and on machines without torch, and it says so
loudly on every load — do not mistake its results for semantic search.

Both produce fixed-width L2-normalised vectors, so the index format and the
search path are identical. Chunk text is always stored, which means switching
embedders re-embeds from disk instead of needing your source files again.

Setup
-----
    pip install numpy                      # required
    pip install sentence-transformers      # for real semantic search

Usage
-----
    python rag.py ingest ./workspace/docs
    python rag.py search "what did we decide about pricing"
    python rag.py list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
HERE = Path(__file__).parent.resolve()

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
HASH_DIM = int(os.environ.get("RAG_HASH_DIM", "4096"))

CHUNK_CHARS = int(os.environ.get("RAG_CHUNK_CHARS", "1000"))
CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "150"))

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".org",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".sh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".html", ".css",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".next", ".idea"}

MAX_FILE_BYTES = int(os.environ.get("RAG_MAX_FILE_BYTES", str(2_000_000)))

# Enough to stop "the" and "and" from dominating the lexical fallback.
_STOPWORDS = frozenset("""
a an and are as at be been but by for from had has have he her his i if in into is it
its of on or our so that the their them then there these they this to was were what
when where which who will with would you your not no do does did can could should
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #
def _l2_normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.maximum(norms, 1e-12, out=norms)  # a zero vector stays zero, no NaN
    return matrix / norms


class HashingEmbedder:
    """Feature-hashed bag of words. Lexical only — no paraphrase matching.

    Sublinear term frequency (1 + log tf) keeps a word repeated twenty times from
    swamping the vector; signed hashing lets collisions cancel instead of always
    accumulating. Fixed width, no fitting, so vectors are independent per chunk
    and safe to persist.
    """

    semantic = False

    def __init__(self, dim: int = HASH_DIM):
        self.dim = dim
        # The width is part of the identity: two hashing embedders with
        # different dims produce incompatible vectors, and an index that cannot
        # tell them apart will happily try to multiply a 256-wide query by a
        # 4096-wide matrix.
        self.name = f"hashing-v1-d{dim}"

    def _one(self, text: str) -> np.ndarray:
        counts: dict[tuple[int, float], float] = {}
        for token in _TOKEN_RE.findall(text.lower()):
            if len(token) < 2 or token in _STOPWORDS:
                continue
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            bucket = value % self.dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            counts[(bucket, sign)] = counts.get((bucket, sign), 0.0) + 1.0

        vector = np.zeros(self.dim, dtype=np.float32)
        for (bucket, sign), count in counts.items():
            vector[bucket] += sign * (1.0 + np.log(count))
        return vector

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalise(np.vstack([self._one(t) for t in texts]))


class SentenceTransformerEmbedder:
    """Real semantic embeddings via sentence-transformers."""

    semantic = True

    def __init__(self, model_name: str = EMBED_MODEL):
        from sentence_transformers import SentenceTransformer

        self.name = f"st:{model_name}"
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self._model.encode(
            texts, convert_to_numpy=True, normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


def load_embedder(prefer_semantic: bool = True):
    """Return the best embedder available, telling the user which one it is.

    A missing package and a model that cannot be downloaded are the same problem
    from here — both mean falling back — so both are caught.
    """
    if prefer_semantic and os.environ.get("RAG_FORCE_HASHING", "").lower() not in ("1", "true", "yes"):
        try:
            embedder = SentenceTransformerEmbedder()
            print(f"[rag] semantic embeddings via {embedder.name} (dim {embedder.dim})")
            return embedder
        except ImportError:
            print("[rag] sentence-transformers not installed — falling back to "
                  "LEXICAL search (keyword overlap only, no paraphrase matching). "
                  "pip install sentence-transformers for real semantic search.")
        except Exception as e:
            print(f"[rag] could not load the embedding model "
                  f"({type(e).__name__}: {e}) — falling back to LEXICAL search "
                  "(keyword overlap only, no paraphrase matching).")
    embedder = HashingEmbedder()
    print(f"[rag] lexical embeddings ({embedder.name}, dim {embedder.dim})")
    return embedder


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into overlapping chunks, breaking at a paragraph or sentence edge.

    Overlap matters: an answer that straddles a hard cut is found by neither
    neighbour otherwise.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    overlap = max(0, min(overlap, size // 2))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            # Prefer a paragraph break, then a sentence end, then whitespace.
            for pattern in ("\n\n", ". ", "\n", " "):
                cut = window.rfind(pattern)
                if cut > size // 2:
                    end = start + cut + len(pattern)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #
class KnowledgeBase:
    """A persistent, searchable index over local documents.

    On disk, inside index_dir:
        chunks.json   the chunk records (source, index, text, file mtime/size)
        vectors.npy   one row per chunk, L2-normalised
        meta.json     which embedder wrote the vectors

    The embedder is loaded lazily, so constructing a KnowledgeBase is cheap and
    a task that never searches never pays for a model.
    """

    def __init__(self, index_dir: Path | str = HERE / "workspace" / "kb_index",
                 embedder=None):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_path = self.index_dir / "chunks.json"
        self.vectors_path = self.index_dir / "vectors.npy"
        self.meta_path = self.index_dir / "meta.json"

        self._embedder = embedder
        self.chunks: list[dict] = []
        self.vectors: np.ndarray | None = None
        self.meta: dict = {}
        self._load()

    # -- embedder ---------------------------------------------------------- #
    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = load_embedder()
        return self._embedder

    # -- persistence ------------------------------------------------------- #
    def _load(self) -> None:
        if self.chunks_path.exists():
            try:
                self.chunks = json.loads(self.chunks_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[warn] index unreadable ({e}); starting empty")
                self.chunks = []
        if self.meta_path.exists():
            try:
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.meta = {}
        if self.vectors_path.exists() and self.chunks:
            try:
                vectors = np.load(self.vectors_path)
                # A stale index is worse than no index: only trust vectors whose
                # row count still matches the chunks they claim to describe.
                if vectors.shape[0] == len(self.chunks):
                    self.vectors = vectors
                else:
                    print("[warn] index out of sync (vectors vs chunks); will re-embed")
            except (OSError, ValueError) as e:
                print(f"[warn] could not load vectors ({e}); will re-embed")

    def _save(self) -> None:
        self.chunks_path.write_text(json.dumps(self.chunks), encoding="utf-8")
        if self.vectors is not None:
            np.save(self.vectors_path, self.vectors)
        self.meta_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    # -- ingest ------------------------------------------------------------ #
    @staticmethod
    def _iter_files(root: Path):
        if root.is_file():
            yield root
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path

    def ingest_path(self, path: Path | str, force: bool = False) -> str:
        """Index a file or a directory tree. Unchanged files are skipped.

        Returns a human-readable summary — forge.py hands it straight back to
        the model, so it says what happened rather than returning a count.
        """
        root = Path(path).expanduser()
        if not root.exists():
            return f"Nothing to ingest: {root} does not exist."

        files = list(self._iter_files(root))
        if not files:
            return (f"No indexable text files under {root}. Looking for: "
                    f"{', '.join(sorted(TEXT_SUFFIXES))}")

        known = {(c["source"], c.get("mtime"), c.get("size")) for c in self.chunks}
        added, skipped, updated, failed = 0, 0, 0, []
        new_records: list[dict] = []
        touched_sources: set[str] = set()

        for file in files:
            try:
                stat = file.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    failed.append(f"{file.name} (too large)")
                    continue
                source = str(file.resolve())
                fingerprint = (source, stat.st_mtime, stat.st_size)
                if not force and fingerprint in known:
                    skipped += 1
                    continue
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                failed.append(f"{file.name} ({e.__class__.__name__})")
                continue

            pieces = chunk_text(text)
            if not pieces:
                continue
            if any(c["source"] == source for c in self.chunks):
                updated += 1
            else:
                added += 1
            touched_sources.add(source)
            for i, piece in enumerate(pieces):
                new_records.append({
                    "source": source,
                    "chunk_index": i,
                    "text": piece,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })

        if not new_records:
            parts = [f"Nothing new to index under {root}"]
            if skipped:
                parts.append(f"{skipped} file(s) already current")
            if failed:
                parts.append(f"skipped: {', '.join(failed[:5])}")
            return ". ".join(parts) + "."

        # Drop the old chunks for any file we just re-read, then append.
        kept_chunks, kept_rows = [], []
        for i, chunk in enumerate(self.chunks):
            if chunk["source"] in touched_sources:
                continue
            kept_chunks.append(chunk)
            kept_rows.append(i)

        kept_vectors = None
        if self.vectors is not None and kept_rows and self._vectors_current():
            kept_vectors = self.vectors[kept_rows]

        new_vectors = self.embedder.embed([r["text"] for r in new_records])

        if kept_vectors is not None and kept_vectors.shape[1] == new_vectors.shape[1]:
            self.vectors = np.vstack([kept_vectors, new_vectors])
            self.chunks = kept_chunks + new_records
        else:
            # No reusable vectors (first run, or the embedder changed) — re-embed
            # everything from the stored text.
            self.chunks = kept_chunks + new_records
            self.vectors = self.embedder.embed([c["text"] for c in self.chunks])

        self.meta = {"embedder": self.embedder.name, "dim": int(self.vectors.shape[1]),
                     "semantic": bool(getattr(self.embedder, "semantic", False))}
        self._save()

        summary = [f"Indexed {len(new_records)} chunks from {added + updated} file(s)"]
        if updated:
            summary.append(f"{updated} re-indexed after changing")
        if skipped:
            summary.append(f"{skipped} unchanged and skipped")
        if failed:
            summary.append(f"could not read: {', '.join(failed[:5])}")
        summary.append(f"{len(self.chunks)} chunks total")
        if not self.meta["semantic"]:
            summary.append("NOTE: lexical index — keyword overlap only")
        return ". ".join(summary) + "."

    def _vectors_current(self) -> bool:
        """True if the stored vectors came from the embedder we're now using.

        The width is checked as well as the name. The name should already be
        enough, but a mismatch here is not a wrong answer — it is a matmul that
        raises — so it is worth catching independently of any embedder keeping
        its promise about naming.
        """
        if not self.meta.get("embedder") or self.meta["embedder"] != self.embedder.name:
            return False
        if self.vectors is not None and self.vectors.shape[1] != self.embedder.dim:
            return False
        return True

    # -- search ------------------------------------------------------------ #
    def search(self, query: str, k: int = 5) -> list[dict]:
        """Return the k most similar chunks, best first.

        Each hit has source, chunk_index, score and text — the shape forge.py
        formats for the model.
        """
        if not self.chunks:
            return []
        if not query or not query.strip():
            return []

        if self.vectors is None or not self._vectors_current() \
                or self.vectors.shape[0] != len(self.chunks):
            # The index was built by a different embedder, or is out of sync.
            # The chunk text is on disk, so rebuild rather than refuse.
            print("[rag] re-embedding the index with the current embedder…")
            self.vectors = self.embedder.embed([c["text"] for c in self.chunks])
            self.meta = {"embedder": self.embedder.name,
                         "dim": int(self.vectors.shape[1]),
                         "semantic": bool(getattr(self.embedder, "semantic", False))}
            self._save()

        q = self.embedder.embed([query])[0]
        # Both sides are L2-normalised, so the dot product is cosine similarity.
        scores = self.vectors @ q

        k = max(1, min(int(k), len(self.chunks)))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]

        return [{
            "source": self.chunks[i]["source"],
            "chunk_index": self.chunks[i]["chunk_index"],
            "score": float(scores[i]),
            "text": self.chunks[i]["text"],
        } for i in top]

    # -- inspect ----------------------------------------------------------- #
    def list_sources(self) -> list[tuple[str, int]]:
        """Indexed documents as (source_path, chunk_count), most chunks first."""
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk["source"]] = counts.get(chunk["source"], 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def clear(self) -> str:
        """Empty the index."""
        n = len(self.chunks)
        self.chunks, self.vectors, self.meta = [], None, {}
        for path in (self.chunks_path, self.vectors_path, self.meta_path):
            path.unlink(missing_ok=True)
        return f"Cleared {n} chunks."

    def __len__(self) -> int:
        return len(self.chunks)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Local document knowledge base.")
    parser.add_argument("command", choices=["ingest", "search", "list", "clear"])
    parser.add_argument("target", nargs="*", help="A path to ingest, or a query")
    parser.add_argument("--index", default=str(HERE / "workspace" / "kb_index"))
    parser.add_argument("-k", type=int, default=5, help="How many passages")
    parser.add_argument("--force", action="store_true", help="Re-index unchanged files")
    args = parser.parse_args()

    kb = KnowledgeBase(index_dir=args.index)

    if args.command == "ingest":
        target = " ".join(args.target) or str(HERE / "workspace" / "docs")
        print(kb.ingest_path(target, force=args.force))

    elif args.command == "search":
        query = " ".join(args.target).strip()
        if not query:
            parser.error("search needs a query")
        hits = kb.search(query, k=args.k)
        if not hits:
            print("No results — the knowledge base may be empty. Try `ingest` first.")
            return
        for n, hit in enumerate(hits, 1):
            print(f"\n[{n}] {Path(hit['source']).name} "
                  f"(chunk {hit['chunk_index']}, score {hit['score']:.3f})")
            print(hit["text"][:500])

    elif args.command == "list":
        sources = kb.list_sources()
        if not sources:
            print("No documents indexed yet.")
            return
        for source, count in sources:
            print(f"{count:4d} chunks  {Path(source).name}")
        print(f"\n{len(sources)} document(s), {len(kb)} chunks, "
              f"embedder {kb.meta.get('embedder', 'unknown')}")

    elif args.command == "clear":
        print(kb.clear())


if __name__ == "__main__":
    main()
