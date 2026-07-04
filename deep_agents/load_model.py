from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from azure.identity import DefaultAzureCredential
from pydantic import BaseModel, Field

llm = AzureAIOpenAIApiChatModel(
    project_endpoint="https://tyb-pro-resource.services.ai.azure.com/api/projects/tyb-pro",
    model="Kimi-K2.6",
    credential=DefaultAzureCredential(),
)