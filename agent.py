from langchain.agents import create_agent
from langchain_openrouter import ChatOpenRouter
import os
from pathlib import Path
from tools import tools
from langchain.agents.middleware import PIIMiddleware, SummarizationMiddleware
from langchain.messages import SystemMessage
from middleware import ContentCheckMiddleware
from ratelimits import limiter

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

with open(PROMPTS_DIR / 'agent.md','r') as f1, open(PROMPTS_DIR / 'input_check.md','r') as f2, open(PROMPTS_DIR / 'output_check.md','r') as f3:
    agent_prompt, input_prompt, output_prompt = f1.read(), f2.read(), f3.read()

entrez_agent = create_agent(
    model=ChatOpenRouter(
        model="openrouter/free",
        temperature=0.5,
        api_key=os.getenv('OPENROUTER_API_KEY_2'),
        rate_limiter=limiter('OPENROUTER_API_KEY_2'),
    ),
    tools=tools,
    middleware=[
        PIIMiddleware(pii_type="email"),
        ContentCheckMiddleware(input_check_prompt=input_prompt, output_check_prompt=output_prompt),
        SummarizationMiddleware(model=ChatOpenRouter(
            model="openrouter/free",
            temperature=0,
            api_key=os.getenv('OPENROUTER_API_KEY_2'),
            rate_limiter=limiter('OPENROUTER_API_KEY_2'),
        ),
        trigger=("tokens", 70000),
        keep=("messages", 15)
    )
    ],
    system_prompt=SystemMessage(content=agent_prompt)
)

