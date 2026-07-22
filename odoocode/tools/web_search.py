# forge/tools/web_search.py
import re
from typing import List

try:
    from duckduckgo_search import DDGS
    _DDG_AVAILABLE = True
except ImportError:
    _DDG_AVAILABLE = False

def web_search(query: str, max_results: int = 3) -> str:
    if not _DDG_AVAILABLE:
        return "(duckduckgo-search not installed)"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return ("\n".join(f"* [{r.get('title','')}]\n  {r.get('body','')[:400]}"
                          for r in results) if results else f"No results for: {query}")
    except Exception as exc:
        return f"(search error: {exc})"

def extract_research_queries(text: str) -> List[str]:
    return [m.group(1).strip()
            for m in re.finditer(r"\[RESEARCH_NEEDED\]:\s*(.+)", text, re.IGNORECASE)]
