# # Mosaic AI Agent Framework: Author and deploy a tool-calling OpenAI agent

# This notebook demonstrates how to author an OpenAI agent that's compatible with Mosaic AI Agent Framework features. In this notebook you learn to:
# - Author a tool-calling OpenAI `ChatAgent`
# - Manually test the agent's output
# - Evaluate the agent using Mosaic AI Agent Evaluation
# - Log and deploy the agent

# To learn more about authoring an agent using Mosaic AI Agent Framework, see Databricks documentation ([AWS](https://docs.databricks.com/aws/generative-ai/agent-framework/author-agent) | [Azure](https://learn.microsoft.com/azure/databricks/generative-ai/agent-framework/create-chat-model)).


import json
import os
from typing import Any, Callable, Dict, Generator, List, Optional
from uuid import uuid4

import backoff
import mlflow
import openai
from databricks.sdk import WorkspaceClient
from databricks_openai import VectorSearchRetrieverTool
# from databricks_openai import VectorSearchRetrieverTool UCFunctionToolkit
# from unitycatalog.ai.core.base import get_uc_function_client
from mlflow.entities import SpanType
from mlflow.pyfunc import ChatAgent
from mlflow.types.agent import (
    ChatAgentChunk,
    ChatAgentMessage,
    ChatAgentResponse,
    ChatContext,
)
from openai import OpenAI
from pydantic import BaseModel
from databricks_ai_bridge.genie import Genie
from databricks.sdk import WorkspaceClient
from brickbots.auth.serviceprincipal import get_service_principal_token

############################################
# Define your LLM endpoint and system prompt
############################################
# TODO: Replace with your model serving endpoint
LLM_ENDPOINT_NAME = "databricks-meta-llama-3-3-70b-instruct"

# TODO: Update with your system prompt
SYSTEM_PROMPT = """
You are a helpful assistant that can reply to questions about NYC Taxi data, using a genie tool to query such data
in natural language.
"""


###############################################################################
## Define tools for your agent, enabling it to retrieve data or take actions
## beyond text generation
## To create and see usage examples of more tools, see
## https://docs.databricks.com/en/generative-ai/agent-framework/agent-tool.html
###############################################################################
class ToolInfo(BaseModel):
    name: str
    spec: dict
    exec_fn: Callable


TOOL_INFOS = []

# You can use UDFs in Unity Catalog as agent tools
# Below, we add the `system.ai.python_exec` UDF, which provides
# a python code interpreter tool to our agent

# TODO: Add additional tools
# UC_TOOL_NAMES = ["system.ai.python_exec"]

# uc_toolkit = UCFunctionToolkit(function_names=UC_TOOL_NAMES)
# uc_function_client = get_uc_function_client()
# for tool_spec in uc_toolkit.tools:
#     tool_name = tool_spec["function"]["name"]
#     udf_name = tool_name.replace("__", ".")

#     # Define a wrapper that accepts kwargs for the UC tool call,
#     # then passes them to the UC tool execution client
#     def execute_uc_tool(**kwargs):
#         function_result = uc_function_client.execute_function(udf_name, kwargs)
#         if function_result.error is not None:
#             return function_result.error
#         else:
#             return function_result.value
    # TOOL_INFOS.append(ToolInfo(name=tool_name, spec=tool_spec, exec_fn=execute_uc_tool))

# Use Databricks vector search indexes as tools
# See https://docs.databricks.com/en/generative-ai/agent-framework/unstructured-retrieval-tools.html#locally-develop-vector-search-retriever-tools-with-ai-bridge
# for details
VECTOR_SEARCH_TOOLS = []

# TODO: Add vector search indexes
# VECTOR_SEARCH_TOOLS.append(
#     VectorSearchRetrieverTool(
#         index_name="",
#         # filters="..."
#     )
# )
for vs_tool in VECTOR_SEARCH_TOOLS:
    TOOL_INFOS.append(
        ToolInfo(
            name=vs_tool.tool["function"]["name"],
            spec=vs_tool.tool,
            exec_fn=vs_tool.execute,
        )
    )


BOTOPS_DATABRICKS_HOST = os.getenv("BOTOPSGENIE_DATABRICKS_HOST")
GENIE_SPACE_ID = os.getenv("BOTOPSGENIE_SPACE_ID")
SERVICE_PRINCIPAL_CLIENT_ID = os.getenv("BOTOPSGENIE_SERVICE_PRINCIPAL_ID")
SERVICE_PRINCIPAL_SECRET = os.getenv("BOTOPSGENIE_SERVICE_PRINCIPAL_SECRET")

print("SERVICE_PRINCIPAL_CLIENT_ID 4 chars: " + str(SERVICE_PRINCIPAL_CLIENT_ID[0:4]))

def get_workspace_client():
    host = BOTOPS_DATABRICKS_HOST
    print(f"host: {host}")
    print("before token SERVICE_PRINCIPAL_CLIENT_ID 4chars: " + str(SERVICE_PRINCIPAL_CLIENT_ID[0:4]))
    token = get_service_principal_token(
        client_id=SERVICE_PRINCIPAL_CLIENT_ID,
        secret=SERVICE_PRINCIPAL_SECRET,
        databricks_host=host,
    )
    print("len(token): " + str(len(token)))
    return WorkspaceClient(
        host=host,
        token=token,
    )

client = get_workspace_client()
genie = Genie(GENIE_SPACE_ID, client=client)


def genietool(question: str) -> str:
  """
  This genie tool can answer questions about NYC taxi trip data

  Args:
      question (str): The question to answer.

  Returns:
      str: The answer to the question
  """
  genie_response = genie.ask_question(question)
  if query_result := genie_response.result:
    return query_result
  return ""


# Define a wrapper that accepts kwargs for the UC tool call,
# then passes them to the UC tool execution client
def execute_uc_tool(**kwargs):
    print("kwargs: " + repr(kwargs))
    return genietool(kwargs["question"])


genie_tool_spec = {'type': 'function',
 'function': {'name': 'taxinyc_genie_tool',
  'strict': True,
  'parameters': {'additionalProperties': False,
   'properties': {'question': {'anyOf': [{'type': 'string'}, {'type': 'null'}],
     'description': 'The question about NYC taxi data',
     'title': 'question'}},
   'title': 'taxinyc_genie_tool__params',
   'type': 'object',
   'required': ['question']},
  'description': "Does a natural language query against a Genie space about NYC taxi data."}}

TOOL_INFOS.append(
        ToolInfo(
            name="taxinyc_genie_tool",
            spec=genie_tool_spec,
            exec_fn=execute_uc_tool,
        )
    )

class ToolCallingAgent(ChatAgent):
    """
    Class representing a tool-calling Agent
    Compatible with Databricks Review App validation requirements.
    """

    def get_tool_specs(self):
        """
        Returns tool specifications in the format OpenAI expects.
        """
        return [tool_info.spec for tool_info in self._tools_dict.values()]
    
    def _ensure_message_compatibility(self, message: ChatAgentMessage) -> ChatAgentMessage:
        """
        Ensure message is compatible with all Review App schema validations.
        """
        # Ensure content field is present (required by ChatAgentChunkSchema and strOutputSchema)
        if not hasattr(message, 'content') or message.content is None:
            message.content = ""
        
        # Ensure id field is present
        if not hasattr(message, 'id') or message.id is None:
            message.id = str(uuid4())
        
        return message

    # @mlflow.trace(span_type=SpanType.TOOL)
    def execute_tool(self, tool_name: str, args: dict) -> Any:
        """
        Executes the specified tool with the given arguments.

        Args:
            tool_name (str): The name of the tool to execute.
            args (dict): Arguments for the tool.

        Returns:
            Any: The tool's output.
        """
        if tool_name not in self._tools_dict:
            raise ValueError(f"Unknown tool: {tool_name}")
        return self._tools_dict[tool_name].exec_fn(**args)

    def __init__(self, llm_endpoint: str, tools: Dict[str, Dict[str, Any]]):
        """
        Initializes the ToolCallingAgent with tools.

        Args:
            tools (Dict[str, Dict[str, Any]]): A dictionary where each key is a tool name,
            and the value is a dictionary containing:
                - "spec" (dict): JSON description of the tool (matches OpenAI format)
                - "function" (Callable): Function that implements the tool logic
        """
        super().__init__()
        self.llm_endpoint = llm_endpoint
        self.workspace_client = WorkspaceClient()
        self.model_serving_client: OpenAI = (
            self.workspace_client.serving_endpoints.get_open_ai_client()
        )
        self._tools_dict = {
            tool.name: tool for tool in tools
        }  # Store tools for later execution
    
    def prepare_messages_for_llm(self, messages: list[ChatAgentMessage]) -> list[dict[str, Any]]:
        """Filter out ChatAgentMessage fields that are not compatible with LLM message formats"""
        compatible_keys = ["role", "content", "name", "tool_calls", "tool_call_id"]
        return [
            {k: v for k, v in m.model_dump_compat(exclude_none=True).items() if k in compatible_keys} for m in messages
        ]

    # @mlflow.trace(span_type=SpanType.AGENT)
    def predict(
        self,
        messages: List[ChatAgentMessage],
        context: Optional[ChatContext] = None,
        custom_inputs: Optional[dict[str, Any]] = None,
    ) -> ChatAgentResponse:
        """
        Primary function that takes a user's request and generates a response.
        Compatible with Databricks Review App validation requirements.
        """
        if len(messages) == 0:
            raise ValueError(
                "The list of `messages` passed to predict(...) must contain at least one message"
            )
        
        # Add system prompt
        all_messages = [
            ChatAgentMessage(role="system", content=SYSTEM_PROMPT)
        ] + messages
        
        response_messages = []
        
        try:
            for message in self.call_and_run_tools(messages=all_messages):
                # Ensure message compatibility with all Review App schemas
                message = self._ensure_message_compatibility(message)
                response_messages.append(message)
        except Exception as e:
            # Handle errors gracefully for playground compatibility
            error_message = ChatAgentMessage(
                role="assistant",
                content=f"I encountered an error: {str(e)}",
                id=str(uuid4())
            )
            response_messages.append(error_message)
        
        # Ensure we always return a valid ChatAgentResponse with messages array
        # This satisfies the ChatAgentResponseSchema validation
        return ChatAgentResponse(messages=response_messages)

    # @mlflow.trace(span_type=SpanType.AGENT)
    def predict_stream(
        self,
        messages: List[ChatAgentMessage],
        context: Optional[ChatContext] = None,
        custom_inputs: Optional[dict[str, Any]] = None,
    ) -> Generator[ChatAgentChunk, None, None]:
        if len(messages) == 0:
            raise ValueError(
                "The list of `messages` passed to predict(...) must contain at least one message"
            )
        
        # Add system prompt
        all_messages = [
            ChatAgentMessage(role="system", content=SYSTEM_PROMPT)
        ] + messages

        try:
            for message in self.call_and_run_tools(messages=all_messages):
                # Ensure message compatibility with all Review App schemas
                message = self._ensure_message_compatibility(message)
                
                # Create chunk with proper delta structure
                chunk = ChatAgentChunk(delta=message)
                yield chunk
        except openai.BadRequestError as e:
            error_data = getattr(e, "response", {}).get("json", lambda: None)()
            if error_data and "external_model_message" in error_data:
                external_error = error_data["external_model_message"].get("error", {})
                if external_error.get("code") == "content_filter":
                    error_message = ChatAgentMessage(
                        role="assistant",
                        content="I'm sorry, I can't respond to that request.",
                        id=str(uuid4())
                    )
                    yield ChatAgentChunk(delta=error_message)
                    return
            raise  # Re-raise if it's not a content filter error
        except Exception as e:
            # Handle other errors gracefully for playground compatibility
            error_message = ChatAgentMessage(
                role="assistant",
                content=f"I encountered an error: {str(e)}",
                id=str(uuid4())
            )
            yield ChatAgentChunk(delta=error_message)

    @backoff.on_exception(backoff.expo, openai.RateLimitError)
    def chat_completion(self, messages: List[ChatAgentMessage]) -> ChatAgentResponse:
        return self.model_serving_client.chat.completions.create(
            model=self.llm_endpoint,
            messages=self.prepare_messages_for_llm(messages),
            tools=self.get_tool_specs(),
        )

    # @mlflow.trace(span_type=SpanType.AGENT)
    def call_and_run_tools(
        self, messages, max_iter=10
    ) -> Generator[ChatAgentMessage, None, None]:
        current_msg_history = messages.copy()
        for i in range(max_iter):
            with mlflow.start_span(span_type="AGENT", name=f"iteration_{i + 1}"):
                try:
                    # Get an assistant response from the model, add it to the running history
                    # and yield it to the caller
                    # NOTE: we perform a simple non-streaming chat completions here
                    # Use the streaming API if you'd like to additionally do token streaming
                    # of agent output.
                    response = self.chat_completion(messages=current_msg_history)
                    llm_message = response.choices[0].message
                    
                    # Ensure content field is present (even if empty for tool calls)
                    message_dict = llm_message.to_dict()
                    if 'content' not in message_dict or message_dict['content'] is None:
                        message_dict['content'] = ""
                    
                    assistant_message = ChatAgentMessage(**message_dict, id=str(uuid4()))
                    assistant_message = self._ensure_message_compatibility(assistant_message)
                    current_msg_history.append(assistant_message)
                    yield assistant_message

                    tool_calls = assistant_message.tool_calls
                    if not tool_calls:
                        return  # Stop streaming if no tool calls are needed

                    # Execute tool calls, add them to the running message history,
                    # and yield their results as tool messages
                    for tool_call in tool_calls:
                        function = tool_call.function
                        args = json.loads(function.arguments)
                        # Cast tool result to a string, since not all tools return as string
                        result = str(self.execute_tool(tool_name=function.name, args=args))
                        tool_call_msg = ChatAgentMessage(
                            role="tool", name=function.name, tool_call_id=tool_call.id, content=result, id=str(uuid4())
                        )
                        tool_call_msg = self._ensure_message_compatibility(tool_call_msg)
                        current_msg_history.append(tool_call_msg)
                        yield tool_call_msg
                        
                except Exception as e:
                    # Handle errors during tool execution
                    error_message = ChatAgentMessage(
                        role="assistant",
                        content=f"Error during processing: {str(e)}",
                        id=str(uuid4())
                    )
                    error_message = self._ensure_message_compatibility(error_message)
                    yield error_message
                    return

        # If we reach max iterations, yield a final message
        final_message = ChatAgentMessage(
           content=f"I'm sorry, I couldn't determine the answer after trying {max_iter} times.",
           role="assistant",
           id=str(uuid4())
        )
        final_message = self._ensure_message_compatibility(final_message)
        yield final_message



# Log the model using MLflow
mlflow.openai.autolog()
BOT = ToolCallingAgent(llm_endpoint=LLM_ENDPOINT_NAME, tools=TOOL_INFOS)
mlflow.models.set_model(BOT)
