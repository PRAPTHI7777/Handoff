import json
from typing import Any, Dict, List, Optional, Tuple

from groq import AsyncGroq

from app.config import settings


FUNCTION_DECLARATIONS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Open an http(s) URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http or https URL",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": "Read the current page and refresh interactive refs.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element using its snapshot ref.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "integer"},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Clear and type text into a field using its snapshot ref.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["ref", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "Choose an option in a select field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "integer"},
                    "value": {"type": "string"},
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user for required missing information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_confirmation",
            "description": "Ask for confirmation before submit, pay, book, delete, or send.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "action": {"type": "string"},
                    "ref": {"type": "integer"},
                },
                "required": ["summary", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human",
            "description": "Pause for OTP, CAPTCHA, login, payment, or verification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete",
            "description": "Finish only after evidence shows the task succeeded.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["summary", "evidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fail",
            "description": "Stop because the task cannot be completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]


class GroqToolClient:
    """Groq local-tool client using the OpenAI-compatible message protocol."""

    def __init__(self) -> None:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required")

        self._client = AsyncGroq(api_key=settings.groq_api_key)

    async def next_tool(
        self,
        contents: List[Dict[str, Any]],
        system_instruction: str,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:

        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": truncate_text(system_instruction, 1800),
            }
        ]

        messages.extend(_bounded_history(contents))

        response = await self._client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            tools=FUNCTION_DECLARATIONS,
            tool_choice="required",
            parallel_tool_calls=False,
            temperature=0,
            max_tokens=300,
        )

        message = response.choices[0].message

        if not message.tool_calls:
            text = (message.content or "").strip()
            raise RuntimeError(
                text or "Model returned no tool call."
            )

        # Handoff advances one browser action at a time. Retaining only this
        # call guarantees the next request pairs it with exactly one result.
        tool_call = message.tool_calls[0]

        name = tool_call.function.name or ""
        raw_args = tool_call.function.arguments or "{}"

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Invalid tool arguments from model: "
                + raw_args
            ) from exc

        model_content = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": raw_args,
                    },
                }
            ],
        }

        return name, args, model_content


def function_response_content(
    name: str,
    payload: Dict[str, Any],
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:

    result: Dict[str, Any] = {
        "role": "tool",
        "name": name,
        "content": truncate_text(json.dumps(payload), 1400),
    }

    if tool_call_id:
        result["tool_call_id"] = tool_call_id

    return result


def user_text_content(text: str) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": text,
    }


def truncate_obs(
    text: str,
    limit: int = 1100,
) -> str:
    return truncate_text(text, limit)


def truncate_text(text: str, limit: int) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "\n...[truncated]"


def _bounded_history(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the initial task and whole assistant/tool pairs, never fragments."""
    if not contents:
        return [{"role": "user", "content": "Call a tool to begin."}]

    initial = contents[0]
    if initial.get("role") != "user":
        initial = {"role": "user", "content": "Continue the browser task."}
    else:
        initial = {
            "role": "user",
            "content": truncate_text(str(initial.get("content", "")), 1600),
        }

    pairs: List[List[Dict[str, Any]]] = []
    tail = contents[1:]
    index = 0
    while index + 1 < len(tail):
        assistant = tail[index]
        tool = tail[index + 1]
        if (
            assistant.get("role") == "assistant"
            and assistant.get("tool_calls")
            and tool.get("role") == "tool"
            and tool.get("tool_call_id")
            and tool.get("tool_call_id")
            == assistant["tool_calls"][0].get("id")
        ):
            pairs.append([assistant, tool])
        index += 2

    result = [initial]
    pair_count = max(1, min(settings.max_history_turns, 6))
    for assistant, tool in pairs[-pair_count:]:
        result.extend([assistant, tool])
    return result
