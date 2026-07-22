# forge/agents/editor.py
import re
from .base import BaseAgent
from ..prompts import PromptLibrary

class EditAgent(BaseAgent):
    def generate_blocks(self, filepath: str, prompt: str, content: str) -> str:
        return self.llm.call(PromptLibrary.edit_system(),
            f"FILE PATH: {filepath}\nUSER PROMPT: {prompt}\n\n"
            f"--- EXISTING ---\n{content}\n--- END ---\n\nGenerate SEARCH/REPLACE blocks.",
            self.config.coder_model)

    @staticmethod
    def apply_blocks(content: str, response: str) -> str:
        if not content.strip():
            m = re.search(r"```[a-z]*\n(.*?)```", response, re.DOTALL)
            return (m.group(1).strip() + "\n") if m else response.strip() + "\n"
        result = content
        for m in re.finditer(r"<<<<\n?(.*?)\n?====\n?(.*?)\n?>>>>", response, re.DOTALL):
            search, replace = m.group(1), m.group(2)
            if search in result:
                result = result.replace(search, replace, 1)
            elif search.strip() and search.strip() in result:
                result = result.replace(search.strip(), replace.strip(), 1)
        return result
