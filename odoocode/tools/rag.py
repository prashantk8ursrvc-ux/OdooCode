# forge/tools/rag.py
"""
Real RAG (Retrieval-Augmented Generation) with vector embeddings.
Uses Ollama's nomic-embed-text for embeddings and SQLite for storage.
"""
import os
import json
import sqlite3
import logging
import hashlib
from pathlib import Path
from typing import List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger("Forge.tools.rag")


@dataclass
class Chunk:
    """A chunk of text with metadata."""
    content: str
    source: str  # file path or skill name
    chunk_id: str = ""
    embedding: List[float] = None

    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = hashlib.md5(f"{self.source}:{self.content[:100]}".encode()).hexdigest()[:12]


class EmbeddingClient:
    """Client for generating embeddings via Ollama."""

    def __init__(self, model: str = "nomic-embed-text:latest"):
        self.model = model
        self._client = None
        self._available = None

    def _get_client(self):
        if self._client is None:
            try:
                import ollama
                self._client = ollama
            except ImportError:
                logger.warning("ollama not installed, RAG embeddings unavailable")
                return None
        return self._client

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        client = self._get_client()
        if not client:
            self._available = False
            return False
        try:
            client.embeddings(model=self.model, prompt="test")
            self._available = True
        except Exception as e:
            logger.warning(f"Embedding model {self.model} not available: {e}")
            self._available = False
        return self._available

    def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        client = self._get_client()
        if not client:
            return []
        # nomic-embed-text has 8192 token limit (~32K chars)
        # Skip if text is likely too long
        if len(text) > 28000:
            logger.warning(f"Text too long for embedding ({len(text)} chars), truncating")
            text = text[:28000]
        try:
            resp = client.embeddings(model=self.model, prompt=text)
            return resp.get("embedding", [])
        except Exception as e:
            logger.warning(f"Embedding failed (len={len(text)}): {e}")
            return []

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts."""
        return [self.embed(text) for text in texts]


class VectorStore:
    """SQLite-based vector store with cosine similarity search."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.expanduser("~"), ".local", "share",
                                    "odoocode", "rag_vectors.db")
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    content TEXT,
                    source TEXT,
                    embedding BLOB,
                    chunk_hash TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_source ON chunks(source)
            """)

    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Serialize embedding to bytes for storage."""
        import struct
        return struct.pack(f'{len(embedding)}f', *embedding)

    def _deserialize_embedding(self, data: bytes) -> List[float]:
        """Deserialize embedding from bytes."""
        import struct
        count = len(data) // 4
        return list(struct.unpack(f'{count}f', data))

    def add_chunk(self, chunk: Chunk, embedding: List[float]):
        """Add a chunk with its embedding to the store."""
        if not embedding:
            return
        chunk.embedding = embedding
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?)",
                (chunk.chunk_id, chunk.content, chunk.source,
                 self._serialize_embedding(embedding),
                 hashlib.md5(chunk.content.encode()).hexdigest())
            )

    def search(self, query_embedding: List[float], top_k: int = 5,
               source_filter: str = None) -> List[Tuple[Chunk, float]]:
        """Search for similar chunks using cosine similarity."""
        if not query_embedding:
            return []

        with sqlite3.connect(self.db_path) as conn:
            if source_filter:
                rows = conn.execute(
                    "SELECT chunk_id, content, source, embedding FROM chunks WHERE source LIKE ?",
                    (f"%{source_filter}%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT chunk_id, content, source, embedding FROM chunks"
                ).fetchall()

        results = []
        query_norm = sum(x*x for x in query_embedding) ** 0.5
        if query_norm == 0:
            return []

        for chunk_id, content, source, emb_data in rows:
            embedding = self._deserialize_embedding(emb_data)
            emb_norm = sum(x*x for x in embedding) ** 0.5
            if emb_norm == 0:
                continue
            # Cosine similarity
            dot_product = sum(a*b for a, b in zip(query_embedding, embedding))
            similarity = dot_product / (query_norm * emb_norm)
            results.append((
                Chunk(content=content, source=source, chunk_id=chunk_id),
                similarity,
            ))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get_stats(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            sources = conn.execute("SELECT DISTINCT source FROM chunks").fetchall()
        return {"total_chunks": count, "sources": len(sources)}

    def clear(self, source: str = None):
        """Clear chunks, optionally filtered by source."""
        with sqlite3.connect(self.db_path) as conn:
            if source:
                conn.execute("DELETE FROM chunks WHERE source LIKE ?", (f"%{source}%",))
            else:
                conn.execute("DELETE FROM chunks")


class SkillRetriever:
    """
    RAG-based skill retriever using vector embeddings.
    Replaces the stub implementation with real vector search.
    """

    def __init__(self, skills_dir: str, embed_model: str = "nomic-embed-text:latest",
                 top_k: int = 5):
        self.skills_dir = Path(skills_dir) if skills_dir else Path(".")
        self.top_k = top_k
        self.embedder = EmbeddingClient(model=embed_model)
        self.store = VectorStore()
        self._indexed = False

    def _chunk_text(self, text: str, chunk_size: int = 150, overlap: int = 30) -> List[str]:
        """Split text into overlapping chunks, respecting token limits."""
        words = text.split()
        chunks = []
        for i in range(0, len(words), chunk_size - overlap):
            chunk = " ".join(words[i:i + chunk_size])
            # Skip chunks that are too short or likely too long for embedding
            if chunk.strip() and len(chunk) > 20:
                chunks.append(chunk)
        return chunks

    def _is_chunk_safe(self, chunk: str, max_tokens: int = 7000) -> bool:
        """Check if a chunk is within embedding token limits."""
        # Rough estimate: 1 token per 4 chars for English
        estimated_tokens = len(chunk) // 4
        return estimated_tokens < max_tokens

    def _index_skills(self):
        """Index all skill files into the vector store."""
        if self._indexed:
            return

        if not self.skills_dir.exists():
            logger.warning(f"Skills directory not found: {self.skills_dir}")
            self._indexed = True
            return

        # Check if embeddings are available
        if not self.embedder.is_available():
            logger.warning("Embedding model not available, RAG disabled")
            self._indexed = True
            return

        # Index markdown files
        skill_files = list(self.skills_dir.glob("**/*.md"))
        if not skill_files:
            logger.info("No skill files found to index")
            self._indexed = True
            return

        logger.info(f"Indexing {len(skill_files)} skill files...")
        indexed = 0
        skipped = 0
        for skill_file in skill_files:
            try:
                content = skill_file.read_text(encoding="utf-8", errors="replace")
                source = str(skill_file.relative_to(self.skills_dir))
                chunks = self._chunk_text(content)

                for i, chunk_text in enumerate(chunks):
                    # Skip chunks that exceed embedding model's context limit
                    if not self._is_chunk_safe(chunk_text):
                        skipped += 1
                        continue
                    embedding = self.embedder.embed(chunk_text)
                    if embedding:
                        chunk = Chunk(content=chunk_text, source=source)
                        self.store.add_chunk(chunk, embedding)
                        indexed += 1
            except Exception as e:
                logger.warning(f"Failed to index {skill_file}: {e}")

        stats = self.store.get_stats()
        logger.info(f"Indexed {indexed} chunks from {stats['sources']} sources "
                    f"({skipped} skipped due to size)")
        self._indexed = True

    def get_relevant_context(self, query: str, top_k: int = None) -> str:
        """Search for relevant skills using vector similarity."""
        self._index_skills()

        if not self.embedder.is_available():
            return ""

        k = top_k or self.top_k
        query_embedding = self.embedder.embed(query)
        if not query_embedding:
            return ""

        results = self.store.search(query_embedding, top_k=k)
        if not results:
            return ""

        lines = []
        for chunk, score in results:
            if score < 0.3:  # Minimum similarity threshold
                continue
            lines.append(f"[{chunk.source}] (similarity={score:.2f})")
            lines.append(chunk.content[:500])
            lines.append("")

        return "\n".join(lines) if lines else ""

    def add_document(self, content: str, source: str):
        """Add a document to the vector store."""
        if not self.embedder.is_available():
            return

        chunks = self._chunk_text(content)
        for chunk_text in chunks:
            embedding = self.embedder.embed(chunk_text)
            if embedding:
                chunk = Chunk(content=chunk_text, source=source)
                self.store.add_chunk(chunk, embedding)

    def clear(self, source: str = None):
        """Clear the vector store."""
        self.store.clear(source)
        self._indexed = False

    def get_stats(self) -> dict:
        return {
            "embedding_available": self.embedder.is_available(),
            "embedding_model": self.embedder.model,
            "store_stats": self.store.get_stats(),
        }
