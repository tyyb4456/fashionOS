from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from azure.identity import DefaultAzureCredential
# from langchain.chat_models import init_chat_model
from langchain_mistralai import ChatMistralAI

from dotenv import load_dotenv
load_dotenv()

model1 = AzureAIOpenAIApiChatModel(
    project_endpoint="https://tyb-pro-resource.services.ai.azure.com/api/projects/tyb-pro",
    model="Kimi-K2.6",
    credential=DefaultAzureCredential(),
)

# model2 = init_chat_model(
#     model="ollama:qwen3.5:4b",
#     reasoning="low",
# )

# Fallback model — swapped Ollama (qwen3.5:4b) -> Mistral. Local qwen was
# behaving inconsistently under TurnAwareModelFallback. mistral-medium-3-5
# supports adjustable reasoning via reasoning_effort, so streaming.py can
# still surface a reasoning trace to ReasoningBlock.jsx when this model
# picks up mid-turn. Requires MISTRAL_API_KEY in .env.
model2 = ChatMistralAI(
    model="mistral-medium-3-5",
    temperature=0,
    model_kwargs={"reasoning_effort": "high"},
)