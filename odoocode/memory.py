# forge/memory.py
"""
OdooCode Persistent Memory System.
File-based memory with BM25 search, checkpoints, and knowledge storage.
Inspired by MiMoCode's memory architecture.
"""
import os
import re
import json
import logging
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("Forge.memory")


@dataclass
class MemoryEntry:
    """A single memory document."""
    path: str
    content: str
    score: float = 0.0
    section: str = ""  # e.g., "rules", "architecture", "patterns"
    last_modified: str = ""


class BM25Search:
    """Lightweight BM25 search over markdown documents."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[MemoryEntry] = []
        self._avgdl: float = 0.0
        self._df: Dict[str, int] = {}  # document frequency per term
        self._N: int = 0

    def _tokenize(self, text: str) -> List[str]:
        """Simple whitespace + lowercasing tokenizer."""
        return re.findall(r'\w+', text.lower())

    def _build_index(self):
        """Build BM25 index from loaded documents."""
        self._N = len(self._docs)
        if self._N == 0:
            return

        total_len = 0
        self._df = {}

        for entry in self._docs:
            tokens = self._tokenize(entry.content)
            total_len += len(tokens)
            seen = set()
            for t in tokens:
                if t not in seen:
                    self._df[t] = self._df.get(t, 0) + 1
                    seen.add(t)

        self._avgdl = total_len / self._N if self._N > 0 else 1.0

    def _idf(self, term: str) -> float:
        """Inverse document frequency."""
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        import math
        return math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)

    def _score_doc(self, query_tokens: List[str], doc: MemoryEntry) -> float:
        """Score a single document against query tokens."""
        doc_tokens = self._tokenize(doc.content)
        doc_len = len(doc_tokens)
        if doc_len == 0:
            return 0.0

        # Term frequency map
        tf = {}
        for t in doc_tokens:
            tf[t] = tf.get(t, 0) + 1

        score = 0.0
        for qt in query_tokens:
            if qt in tf:
                term_freq = tf[qt]
                idf = self._idf(qt)
                numerator = term_freq * (self.k1 + 1)
                denominator = term_freq + self.k1 * (1 - self.b + self.b * doc_len / self._avgdl)
                score += idf * numerator / denominator

        # Bonus for title/header matches
        first_line = doc.content.split('\n')[0].lower()
        for qt in query_tokens:
            if qt in first_line:
                score += 2.0

        return score

    def search(self, query: str, top_k: int = 5, min_score: float = 0.1) -> List[MemoryEntry]:
        """Search documents with BM25 ranking."""
        if not self._docs:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        results = []
        for doc in self._docs:
            score = self._score_doc(query_tokens, doc)
            if score >= min_score:
                results.append(MemoryEntry(
                    path=doc.path,
                    content=doc.content,
                    score=score,
                    section=doc.section,
                    last_modified=doc.last_modified,
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]


class MemoryManager:
    """
    Persistent file-based memory system.

    Structure:
      {memory_dir}/
        MEMORY.md              — project-level rules and architecture
        knowledge/
          patterns.md          — learned patterns from critic feedback
          odoo_rules.md        — discovered Odoo best practices
        sessions/
          {session_id}/
            checkpoint.md      — structured session state
            notes.md           — free-form scratchpad
    """

    def __init__(self, memory_dir: str = ".odoo_memory"):
        self.base_dir = Path(memory_dir)
        self._search = BM25Search()
        self._loaded = False
        self._session_id: Optional[str] = None

    def _ensure_dirs(self):
        """Create memory directory structure."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "knowledge").mkdir(exist_ok=True)
        (self.base_dir / "sessions").mkdir(exist_ok=True)

    def init(self, session_id: str = None):
        """Initialize memory system for a session."""
        self._ensure_dirs()
        self._session_id = session_id

        # Create MEMORY.md if it doesn't exist
        memory_file = self.base_dir / "MEMORY.md"
        if not memory_file.exists():
            memory_file.write_text(
                "# Project Memory\n\n"
                "## Rules\n"
                "_Project-level rules that every session must respect._\n\n"
                "## Architecture\n"
                "_Major design choices with rationale._\n\n"
                "## Knowledge\n"
                "_Durable facts discovered during development._\n\n",
                encoding="utf-8",
            )
            logger.info(f"Created {memory_file}")

        # Create knowledge files
        patterns_file = self.base_dir / "knowledge" / "patterns.md"
        if not patterns_file.exists():
            patterns_file.write_text(
                "# Learned Patterns\n\n"
                "_Patterns extracted from successful critic reviews and fixes._\n\n",
                encoding="utf-8",
            )

        # Create session directory if session_id provided
        if session_id:
            session_dir = self.base_dir / "sessions" / session_id
            session_dir.mkdir(exist_ok=True)
            (session_dir / "notes.md").write_text(
                f"# Session Notes — {session_id}\n\n",
                encoding="utf-8",
            )

        self._load_all()
        logger.info(f"Memory initialized at {self.base_dir} (session={session_id})")

    def _load_all(self):
        """Load all memory files into search index."""
        self._search._docs = []

        # Load MEMORY.md sections
        memory_file = self.base_dir / "MEMORY.md"
        if memory_file.exists():
            content = memory_file.read_text(encoding="utf-8")
            sections = self._split_sections(content)
            for section_name, section_content in sections:
                self._search._docs.append(MemoryEntry(
                    path="MEMORY.md",
                    content=section_content,
                    section=section_name,
                    last_modified=self._get_mtime_str(memory_file),
                ))

        # Load knowledge files
        knowledge_dir = self.base_dir / "knowledge"
        if knowledge_dir.exists():
            for f in knowledge_dir.glob("*.md"):
                content = f.read_text(encoding="utf-8")
                sections = self._split_sections(content)
                for section_name, section_content in sections:
                    self._search._docs.append(MemoryEntry(
                        path=f"knowledge/{f.name}",
                        content=section_content,
                        section=section_name,
                        last_modified=self._get_mtime_str(f),
                    ))

        # Load session notes (current and recent)
        sessions_dir = self.base_dir / "sessions"
        if sessions_dir.exists():
            for session_dir in sorted(sessions_dir.iterdir(), reverse=True)[:5]:
                if not session_dir.is_dir():
                    continue
                for f in session_dir.glob("*.md"):
                    content = f.read_text(encoding="utf-8")
                    self._search._docs.append(MemoryEntry(
                        path=f"sessions/{session_dir.name}/{f.name}",
                        content=content,
                        section=f"session/{session_dir.name}",
                        last_modified=self._get_mtime_str(f),
                    ))

        self._search._build_index()
        self._loaded = True

    def _split_sections(self, content: str) -> List[Tuple[str, str]]:
        """Split markdown into named sections based on ## headers."""
        sections = []
        current_name = "general"
        current_lines = []

        for line in content.split('\n'):
            if line.startswith('## '):
                if current_lines:
                    sections.append((current_name, '\n'.join(current_lines)))
                current_name = line[3:].strip().lower()
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            sections.append((current_name, '\n'.join(current_lines)))

        return sections if sections else [("general", content)]

    def _get_mtime_str(self, path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        except Exception:
            return ""

    # === Search API ===

    def search(self, query: str, top_k: int = 5, min_score: float = 0.1) -> List[MemoryEntry]:
        """Search memory with BM25 ranking."""
        if not self._loaded:
            self._load_all()
        return self._search.search(query, top_k=top_k, min_score=min_score)

    def search_context(self, query: str, max_tokens: int = 2000) -> str:
        """Search and format results as context string for LLM injection."""
        results = self.search(query, top_k=3)
        if not results:
            return ""

        lines = ["[PROJECT MEMORY — persistent knowledge]\n"]
        for entry in results:
            # Truncate long entries
            content = entry.content[:800]
            if len(entry.content) > 800:
                content += "\n...[truncated]"
            lines.append(f"--- {entry.path} [{entry.section}] (score={entry.score:.2f}) ---")
            lines.append(content)
            lines.append("")

        context = "\n".join(lines)
        # Rough token estimate: ~4 chars per token
        if len(context) > max_tokens * 4:
            context = context[:max_tokens * 4] + "\n...[context truncated]"

        return context

    # === Write API ===

    def read_memory(self) -> str:
        """Read the main MEMORY.md file."""
        memory_file = self.base_dir / "MEMORY.md"
        if memory_file.exists():
            return memory_file.read_text(encoding="utf-8")
        return ""

    def append_memory(self, section: str, content: str):
        """Append content to a section in MEMORY.md."""
        memory_file = self.base_dir / "MEMORY.md"
        text = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""

        header = f"## {section}"
        if header in text:
            # Insert after the header
            idx = text.index(header) + len(header)
            # Find next ## or end of file
            next_section = text.find("\n## ", idx)
            if next_section == -1:
                next_section = len(text)
            insert_point = text.find("\n", idx) + 1
            text = text[:insert_point] + f"\n{content}\n" + text[insert_point:]
        else:
            # Add new section
            text += f"\n{header}\n{content}\n"

        memory_file.write_text(text, encoding="utf-8")
        self._loaded = False  # Force re-index on next search

    def add_pattern(self, pattern_type: str, description: str, example: str = ""):
        """Add a learned pattern to knowledge/patterns.md."""
        patterns_file = self.base_dir / "knowledge" / "patterns.md"
        text = patterns_file.read_text(encoding="utf-8") if patterns_file.exists() else "# Learned Patterns\n\n"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### [{timestamp}] {pattern_type}\n{description}\n"
        if example:
            entry += f"\n```\n{example}\n```\n"

        text += entry
        patterns_file.write_text(text, encoding="utf-8")
        self._loaded = False

    def add_knowledge(self, key: str, value: str):
        """Add a knowledge entry to MEMORY.md ## Knowledge section."""
        self.append_memory("Knowledge", f"- **{key}**: {value}")

    # === Checkpoint API ===

    def save_checkpoint(self, session_id: str, state: dict):
        """Save session checkpoint as structured markdown."""
        session_dir = self.base_dir / "sessions" / session_id
        session_dir.mkdir(exist_ok=True)

        checkpoint_file = session_dir / "checkpoint.md"
        lines = [
            f"# Session Checkpoint — {session_id}",
            f"_Last updated: {datetime.now().isoformat()}_\n",
        ]

        for key, value in state.items():
            if isinstance(value, list):
                lines.append(f"## {key}")
                for item in value:
                    lines.append(f"- {item}")
            elif isinstance(value, dict):
                lines.append(f"## {key}")
                for k, v in value.items():
                    lines.append(f"- **{k}**: {v}")
            else:
                lines.append(f"## {key}")
                lines.append(str(value))
            lines.append("")

        checkpoint_file.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Checkpoint saved: {checkpoint_file}")

    def load_checkpoint(self, session_id: str) -> Optional[dict]:
        """Load session checkpoint."""
        checkpoint_file = self.base_dir / "sessions" / session_id / "checkpoint.md"
        if not checkpoint_file.exists():
            return None

        content = checkpoint_file.read_text(encoding="utf-8")
        state = {}
        current_key = None
        current_lines = []

        for line in content.split('\n'):
            if line.startswith('## '):
                if current_key:
                    state[current_key] = '\n'.join(current_lines).strip()
                current_key = line[3:].strip()
                current_lines = []
            elif current_key:
                current_lines.append(line)

        if current_key:
            state[current_key] = '\n'.join(current_lines).strip()

        return state

    def save_notes(self, session_id: str, notes: str):
        """Save session notes."""
        session_dir = self.base_dir / "sessions" / session_id
        session_dir.mkdir(exist_ok=True)
        (session_dir / "notes.md").write_text(
            f"# Session Notes — {session_id}\n\n{notes}",
            encoding="utf-8",
        )

    def append_notes(self, session_id: str, note: str):
        """Append to session notes."""
        session_dir = self.base_dir / "sessions" / session_id
        notes_file = session_dir / "notes.md"
        if notes_file.exists():
            existing = notes_file.read_text(encoding="utf-8")
        else:
            existing = f"# Session Notes — {session_id}\n\n"

        timestamp = datetime.now().strftime("%H:%M")
        existing += f"\n## [{timestamp}]\n{note}\n"
        notes_file.write_text(existing, encoding="utf-8")

    # === Dream (Memory Consolidation) ===

    def dream(self, session_summaries: List[str] = None) -> str:
        """
        Consolidate session traces into persistent knowledge.
        Similar to MiMoCode's /dream command.
        """
        self._ensure_dirs()

        # Collect all session notes
        sessions_dir = self.base_dir / "sessions"
        all_notes = []
        if sessions_dir.exists():
            for session_dir in sorted(sessions_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                notes_file = session_dir / "notes.md"
                if notes_file.exists():
                    all_notes.append(notes_file.read_text(encoding="utf-8"))

        if session_summaries:
            all_notes.extend(session_summaries)

        if not all_notes:
            return "No session traces to consolidate."

        # Extract patterns and knowledge
        patterns = []
        knowledge = []
        rules = []

        for note in all_notes:
            # Look for patterns (marked with **, or after "Pattern:", "Rule:", etc.)
            for match in re.finditer(r'\*\*(.+?)\*\*[:\s]+(.+)', note):
                key, value = match.group(1), match.group(2)
                knowledge.append(f"- **{key}**: {value}")

            for match in re.finditer(r'(?:Pattern|Rule|Important)[:\s]+(.+)', note, re.IGNORECASE):
                rules.append(f"- {match.group(1)}")

            for match in re.finditer(r'(?:Issue|Bug|Problem)[:\s]+(.+)', note, re.IGNORECASE):
                patterns.append(f"- {match.group(1)}")

        # Write consolidated knowledge
        if knowledge:
            self.append_memory("Knowledge", "\n".join(knowledge))

        if rules:
            self.append_memory("Rules", "\n".join(rules))

        if patterns:
            self.add_pattern("session_patterns", "\n".join(patterns))

        summary = (
            f"Dream complete: consolidated {len(all_notes)} session(s), "
            f"{len(knowledge)} knowledge entries, {len(rules)} rules, "
            f"{len(patterns)} patterns."
        )
        logger.info(summary)
        return summary

    # === Cleanup ===

    def list_sessions(self) -> List[str]:
        """List all session IDs."""
        sessions_dir = self.base_dir / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted([
            d.name for d in sessions_dir.iterdir() if d.is_dir()
        ], reverse=True)

    def cleanup_old_sessions(self, keep_last: int = 10):
        """Remove old session directories, keeping the most recent N."""
        sessions = self.list_sessions()
        for old_session in sessions[keep_last:]:
            session_dir = self.base_dir / "sessions" / old_session
            if session_dir.exists():
                import shutil
                shutil.rmtree(session_dir)
                logger.info(f"Cleaned up old session: {old_session}")

    def get_stats(self) -> dict:
        """Get memory system statistics."""
        return {
            "memory_file": str(self.base_dir / "MEMORY.md"),
            "knowledge_files": len(list((self.base_dir / "knowledge").glob("*.md"))) if (self.base_dir / "knowledge").exists() else 0,
            "sessions": len(self.list_sessions()),
            "indexed_documents": len(self._search._docs),
            "session_id": self._session_id,
        }


class CodeKnowledgeStore:
    """
    Incremental Codebase Knowledge Store & RAG Retrieval Engine.
    Indexes models, fields, security groups, actions, and assets as code is generated,
    enabling token-efficient semantic context retrieval for downstream file generation.
    """

    def __init__(self):
        self.search_engine = BM25Search()
        self.indexed_files: Dict[str, str] = {}

    def index_file(self, filepath: str, content: str):
        """Index a generated file into semantic memory."""
        self.indexed_files[filepath] = content
        
        # Build semantic summary entry
        summary_text = f"FILE: {filepath}\n"
        
        if filepath.endswith(".py") and "models/" in filepath and not filepath.endswith("__init__.py"):
            m_name = re.search(r"_name\s*=\s*['\"]([^'\"]+)['\"]", content)
            fields = re.findall(r'^\s+(\w+)\s*=\s*fields\.', content, re.MULTILINE)
            model = m_name.group(1) if m_name else Path(filepath).stem
            summary_text += f"TYPE: Model\nNAME: {model}\nDECLARED_FIELDS: {', '.join(fields)}\n"
        elif filepath.endswith(".xml"):
            xml_ids = re.findall(r'id=["\']([^"\']+)["\']', content)
            summary_text += f"TYPE: XML\nDECLARED_XML_IDS: {', '.join(xml_ids[:20])}\n"
        else:
            summary_text += f"TYPE: Source\nCONTENT: {content[:1000]}\n"

        entry = MemoryEntry(
            path=filepath,
            content=summary_text,
            section="code_knowledge",
            last_modified=datetime.now().isoformat(),
        )
        self.search_engine.add_document(entry)

    def retrieve_context(self, target_filepath: str, max_tokens: int = 2000) -> str:
        """
        Retrieve ultra-compact, token-efficient semantic context relevant to target_filepath.
        """
        if not self.indexed_files:
            return ""

        # Construct semantic query terms based on target file path
        target_stem = Path(target_filepath).stem.replace("_views", "").replace("_data", "")
        query_terms = f"{target_stem} model fields actions security views"

        relevant_entries = self.search_engine.search(query_terms, limit=5)
        if not relevant_entries:
            # Fall back to returning all indexed summaries
            snippets = [doc.content for doc in self.search_engine._docs]
            return "\n\n".join(snippets[:5])

        snippets = [doc.content for doc, score in relevant_entries if score > 0]
        if not snippets:
            snippets = [doc.content for doc in self.search_engine._docs]

        context = "[RETRIEVED KNOWLEDGE INDEX — relevant symbols for file generation]\n"
        context += "\n\n".join(snippets[:5])
        context += "\n[END RETRIEVED KNOWLEDGE INDEX]\n"
        return context
