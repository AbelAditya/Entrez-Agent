from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.messages import AIMessage, HumanMessage
from langchain_openrouter import ChatOpenRouter
from ratelimits import limiter
from store import KeyValueStore, VerdictCache
import os

from typing import Optional

BLOCKED = "BLOCKED"
ALLOWED = "ALLOWED"


def latest_human_text(messages) -> Optional[str]:
    """Text of the most recent human turn, or None if there isn't one.

    Scanning from the end rather than taking messages[0] means a follow-up
    request is what gets checked, not whatever opened the conversation; and
    rather than messages[-1], because a resumed run can end on a tool or AI
    message.
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.text if hasattr(message, "text") else str(message.content)
    return None


class ContentCheckMiddleware(AgentMiddleware):
    def __init__(self, input_check_prompt: Optional[str], output_check_prompt: Optional[str], model: str = "cohere/north-mini-code:free", store: Optional[KeyValueStore] = None):
        super().__init__()
        self.input_prompt = input_check_prompt
        self.output_prompt = output_check_prompt
        self.model = ChatOpenRouter(
            model=model,
            temperature=0,
            api_key=os.getenv('OPENROUTER_API_KEY'),
            rate_limiter=limiter('OPENROUTER_API_KEY'),
        )
        store = store or KeyValueStore()
        self.input_cache = VerdictCache(store, "in", input_check_prompt, model)
        self.output_cache = VerdictCache(store, "out", output_check_prompt, model)

    def _check(self, cache: VerdictCache, check_prompt: str, label: str, text: str) -> str:
        """Return ALLOWED/BLOCKED for `text`, spending a model request only on a miss.

        A failed call is never cached: one rate-limit error must not freeze a
        verdict for the life of the entry.
        """
        cached = cache.get(text)
        if cached:
            return cached

        response = self.model.invoke(f"{check_prompt}\n{label}: {text}")
        verdict = BLOCKED if response.content.strip().upper().startswith(BLOCKED) else ALLOWED
        cache.set(text, verdict)
        return verdict

    @hook_config(can_jump_to=['end'])
    def before_agent(self, state, runtime):
        if not self.input_prompt:
            return None

        text = latest_human_text(state['messages'])
        if not text:  # nothing from the user to check, e.g. a resumed run
            return None

        if self._check(self.input_cache, self.input_prompt, "User Query", text) == BLOCKED:
            return {
                    "messages": [{
                        "role": "assistant",
                        "content": "I cannot process requests containing inappropriate content. Please rephrase your request."
                    }],
                    "jump_to": "end"
                }

        return None

    def after_agent(self, state, runtime):
        if not self.output_prompt:
            return None

        last = state['messages'][-1]
        text = last.text if hasattr(last, "text") else str(last.content)
        if not text:
            return None

        if self._check(self.output_cache, self.output_prompt, "Agent Output", text) == BLOCKED:
            # Reuse the id so add_messages replaces the blocked answer instead of
            # appending the refusal after it.
            return {
                    "messages": [AIMessage(
                        id=last.id,
                        content="Sorry I cannot help you at the moment, please try again.",
                    )]
                }

        return None
