"""Interactive chat with the Entrez agent.

Keeps the conversation in memory across turns, so follow-ups like "only from
2023" or "show me the abstracts for those" work. Commands: /new, /verbose,
/exit.
"""

from dotenv import load_dotenv

load_dotenv()  # before importing agent: it builds its models at import time

from agent import entrez_agent  # noqa: E402
from langchain.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

BANNER = """Entrez agent. Ask a biomedical question.
  /new      start a fresh conversation
  /verbose  show tool results as well as tool calls
  /exit     quit (Ctrl-D also works)"""


def show(message, verbose: bool) -> None:
    """Print one new message: tool calls as one line each, answers in full."""
    if isinstance(message, AIMessage):
        for call in message.tool_calls or []:
            args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items())
            print(f"  → {call['name']}({args[:160]})")
        text = message.text if hasattr(message, "text") else str(message.content)
        if text.strip():
            print(f"\nagent> {text.strip()}")
    elif isinstance(message, ToolMessage) and verbose:
        content = str(message.content)
        print(f"    {content[:500]}{'…' if len(content) > 500 else ''}")


def main() -> None:
    print(BANNER)
    messages: list = []
    seen: set[str] = set()
    verbose = False

    while True:
        try:
            query = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not query:
            continue
        if query in ("/exit", "/quit"):
            break
        if query == "/new":
            messages, seen = [], set()
            print("(new conversation)")
            continue
        if query == "/verbose":
            verbose = not verbose
            print(f"(verbose {'on' if verbose else 'off'})")
            continue

        messages.append(HumanMessage(content=query))
        try:
            result = entrez_agent.invoke({"messages": messages})
        except KeyboardInterrupt:
            print("\n(interrupted)")
            messages.pop()  # drop the unanswered turn so history stays consistent
            continue
        except Exception as exc:
            print(f"\n[{type(exc).__name__}] {exc}")
            messages.pop()
            continue

        messages = result["messages"]
        # Summarisation can rewrite history, so track ids rather than positions.
        for message in messages:
            if message.id and message.id not in seen:
                seen.add(message.id)
                show(message, verbose)


if __name__ == "__main__":
    main()
