from typing_extensions import Literal

from langchain.messages import SystemMessage, ToolMessage
from tools import model_with_tools, tools_by_name, model
from langgraph.graph import END
from models import MessagesState

from nemoguardrails import LLMRails, RailsConfig


f = open("system_prompt.md","r")
sys_prompt = f.read()

def oracle(state: MessagesState):   
    config = RailsConfig.from_path("guardrails")
    rails = LLMRails(config=config)

    return {
        "messages": [
            rails.generate(
                [
                SystemMessage(content=sys_prompt)
            ] + state['messages']
            )
        ]
    }


def tool_node(state: MessagesState):
    msgs = state['messages']
    tools_called = msgs[-1].tool_calls

    result = []

    for i in tools_called:
        tool = tools_by_name[i["name"]]
        out = tool.invoke(i["args"])

        result.append(ToolMessage(content=out,tool_call_id=i["id"]))

    return {"messages":result}


def should_continue(state: MessagesState) -> Literal["tool_node", "__end__"]:

    last_msg = state['messages'][-1]

    if last_msg.tool_calls:
        return "tool_node"

    return END

def pretty_output(state:MessagesState)->str:
    return {
        "messages": [
            model.invoke(
                [SystemMessage(content="Your job is to take the results recieved from the previous tool calls and refine them into a natural language response to the human query.")]
                + state['messages']
            )
        ]
    }