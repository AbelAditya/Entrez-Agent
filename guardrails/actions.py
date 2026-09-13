from nemoguardrails.actions import action
from tools import model

@action()
async def generate_refusal(user_prompt: str):
    prompt = f"""
The user's request was rejected because it is outside the scope of a biomedical Entrez assistant. Politely decline in 1-2 sentences, don't lecture, and suggest a related in-scope query they could ask instead.\n\n User request: {user_prompt}
"""
    return (await model.ainvoke(prompt)).content