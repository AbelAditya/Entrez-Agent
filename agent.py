from langchain.agents import create_agent
from langchain_openrouter import ChatOpenRouter
import os
from tools import tools
from langchain.agents.middleware import PIIMiddleware
from langchain.messages import SystemMessage
from middleware import ContentCheckMiddleware

with open('prompts/agent.md','r') as f1, open('prompts/input_check.md','r') as f2, open('prompts/output_check.md','r') as f3:
    agent_prompt, input_prompt, output_prompt = f1.read(), f2.read(), f3.read()

entrez_agent = create_agent(
    model=ChatOpenRouter(
        model="cohere/north-mini-code:free",
        temperature=0.5,
        api_key=os.getenv('OPENROUTER_API_KEY'),
    ),
    tools=tools,
    middleware=[
        PIIMiddleware(pii_type="email"),
        ContentCheckMiddleware(input_check_prompt=input_prompt, output_check_prompt=output_prompt)
    ],
    system_prompt=SystemMessage(content=agent_prompt)
)

