# forge/agents/base.py

class BaseAgent:
    def __init__(self, llm, config):
        self.llm = llm
        self.config = config

    def run(self, **kwargs):
        """Run the agent. Override in subclasses."""
        pass
