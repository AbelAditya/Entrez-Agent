from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.messages import AIMessage
from langchain_openrouter import ChatOpenRouter
import os
from dotenv import load_dotenv

from typing import Optional

load_dotenv()

class ContentCheckMiddleware(AgentMiddleware):
    def __init__(self, input_check_prompt: Optional[str], output_check_prompt: Optional[str], model: str = "cohere/north-mini-code:free"):
        super().__init__()
        self.input_prompt = input_check_prompt
        self.output_prompt = output_check_prompt
        self.model = ChatOpenRouter(
            model=model,
            temperature=0,
            api_key=os.getenv('OPENROUTER_API_KEY'),
        )

    @hook_config(can_jump_to=['end'])
    def before_agent(self, state, runtime):
        if not self.input_prompt:
            return None

        first = state['messages'][0]
        prompt = self.input_prompt + f"\nUser Query: {first.content}"

        resp = self.model.invoke(prompt)

        if resp.content.strip().upper().startswith("BLOCKED"):
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
        prompt = self.output_prompt + f"\nAgent Output: {last.content}"

        resp = self.model.invoke(prompt)

        if resp.content.strip().upper().startswith("BLOCKED"):
            return {
                    "messages": [AIMessage(
                        id=last.id,
                        content="Sorry I cannot help you at the moment, please try again.",
                    )]
                }

        return None