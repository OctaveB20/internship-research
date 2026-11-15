"""Faux clients IA pour les tests (aucun appel réseau)."""
from src import llm


class FakeLLM:
    """`answers` : dicts (ou None) renvoyés successivement par structured()."""
    provider = "Fake"

    def __init__(self, answers=None, chat_script=None, tokens=(100, 50)):
        self.answers = list(answers or [])
        self.chat_script = chat_script or []
        self.calls = 0
        self.tokens = tokens

    def structured(self, system, user, tool):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer, llm.Usage(*self.tokens)

    def chat(self, system, history, tools, run_tool, max_steps=6):
        result = llm.ChatResult("")
        outputs = []
        for name, args in self.chat_script:
            outputs.append(run_tool(name, args))
            result.tool_calls.append({"name": name, "args": args})
        result.text = f"{len(outputs)} outil(s) appelé(s)"
        result.outputs = outputs
        return result
