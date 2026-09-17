from agent import entrez_agent
from langchain.messages import HumanMessage

from dotenv import load_dotenv

load_dotenv()

query = input("Enter you query: ")
msgs = [HumanMessage(content=query)]

result = entrez_agent.invoke({"messages":msgs})

for m in result['messages']:
    m.pretty_print()