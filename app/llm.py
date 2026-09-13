from typing import Any

from google import genai
from google.genai import types

from app.config import settings

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="navigate",
        description="Open an http(s) URL in the browser.",
        parameters_json_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full http or https URL"}},
            "required": ["url"],
        },
    ),
    types.FunctionDeclaration(
        name="snapshot",
        description="Re-read the current page and refresh numbered interactive refs.",
        parameters_json_schema={"type": "object", "properties": {}},
    ),
    types.FunctionDeclaration(
        name="click",
        description="Click an interactive element by its current snapshot ref number.",
        parameters_json_schema={
            "type": "object",
            "properties": {"ref": {"type": "integer", "description": "Snapshot ref, e.g. 3"}},
            "required": ["ref"],
        },
    ),
    types.FunctionDeclaration(
        name="type",
        description="Clear and type text into a field identified by snapshot ref.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["ref", "text"],
        },
    ),
    types.FunctionDeclaration(
        name="select",
        description="Choose an option in a select/combobox by snapshot ref. Prefer option value, then visible label.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "value": {"type": "string"},
            },
            "required": ["ref", "value"],
        },
    ),
    types.FunctionDeclaration(
        name="press_key",
        description="Press a keyboard key on the page.",
        parameters_json_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    ),
    types.FunctionDeclaration(
        name="ask_user",
        description="Pause and ask the user for missing information. Use only when the data is required and not in the profile.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short field names the UI should collect, e.g. email",
                },
            },
            "required": ["question"],
        },
    ),
    types.FunctionDeclaration(
        name="request_confirmation",
        description="Pause for explicit user confirmation before a consequential action such as submit, pay, book, delete, or send.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "action": {"type": "string"},
                "ref": {"type": "integer", "description": "Optional snapshot ref that will be clicked after confirm"},
            },
            "required": ["summary", "action"],
        },
    ),
    types.FunctionDeclaration(
        name="request_human",
        description="Pause for a human to handle OTP, CAPTCHA, login, payment, or other verification. Browser stays open.",
        parameters_json_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    ),
    types.FunctionDeclaration(
        name="complete",
        description="Finish only after the current snapshot shows the task succeeded. evidence must quote page text or URL proof.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["summary", "evidence"],
        },
    ),
    types.FunctionDeclaration(
        name="fail",
        description="Stop because the task cannot be completed.",
        parameters_json_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    ),
]


class GeminiToolClient:
    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required")
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._tool = types.Tool(function_declarations=FUNCTION_DECLARATIONS)

    async def next_tool(
        self,
        contents: list[types.Content],
        system_instruction: str,
    ) -> tuple[str, dict[str, Any], types.Content]:
        response = await self._client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0,
                tools=[self._tool],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="ANY")
                ),
            ),
        )
        calls = list(response.function_calls or [])
        if not calls:
            text = (response.text or "").strip() or "Model returned no tool call."
            raise RuntimeError(text)
        call = calls[0]
        name = call.name or ""
        raw_args = call.args or {}
        args = dict(raw_args) if not isinstance(raw_args, dict) else dict(raw_args)
        model_content = None
        if response.candidates:
            model_content = response.candidates[0].content
        if model_content is None:
            model_content = types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
            )
        return name, args, model_content


def function_response_content(name: str, payload: dict[str, Any]) -> types.Content:
    return types.Content(
        role="tool",
        parts=[types.Part.from_function_response(name=name, response=payload)],
    )


def user_text_content(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def truncate_obs(text: str, limit: int = 6000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]"
