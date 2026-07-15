from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from azure.identity import DefaultAzureCredential
from langchain.chat_models import init_chat_model

from dotenv import load_dotenv
load_dotenv()

model1 = AzureAIOpenAIApiChatModel(
    project_endpoint="https://tyb-pro-resource.services.ai.azure.com/api/projects/tyb-pro",
    model="Kimi-K2.6",
    credential=DefaultAzureCredential(),
)

model2 = init_chat_model(
    model="ollama:qwen3.5:4b",
    reasoning="low",
)