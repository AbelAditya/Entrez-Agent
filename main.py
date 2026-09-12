from langgraph.graph import StateGraph, START, END
from models import MessagesState
from nodes import oracle, tool_node, should_continue
from langchain.messages import HumanMessage

graph = StateGraph(MessagesState)

# Adding nodes
graph.add_node("oracle",oracle)
graph.add_node("tool_node",tool_node)

# Adding Edges
graph.add_edge(START, "oracle")
graph.add_conditional_edges(
    "oracle",
    should_continue,
    ["tool_node",END]
)
graph.add_edge("tool_node","oracle")

agent = graph.compile()

a = input("Enter Query: ")
messages = [HumanMessage(content=a)]
messages = agent.invoke({"messages":messages})

for m in messages['messages']:
    m.pretty_print()


