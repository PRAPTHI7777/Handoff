import re
from typing import Any, Optional

from pydantic import ValidationError

from app.browser.session import BrowserSession
from app.schemas import (
    AskUserArgs,
    CompleteArgs,
    ConfirmationArgs,
    FailArgs,
    HumanArgs,
    NavigateArgs,
    PressKeyArgs,
    RefArgs,
    SelectArgs,
    TaskStatus,
    ToolResult,
    TypeArgs,
)

BROWSER_TOOLS = {"navigate", "snapshot", "click", "type", "select", "press_key"}
PAUSE_TOOLS = {"ask_user", "request_confirmation", "request_human"}
TERMINAL_TOOLS = {"complete", "fail"}
ALLOWED_TOOLS = BROWSER_TOOLS | PAUSE_TOOLS | TERMINAL_TOOLS

CONSEQUENTIAL_RE = re.compile(
    r"\b(submit|pay|payment|purchase|buy|book|booking|delete|remove|send|confirm|checkout|place order|subscribe)\b",
    re.IGNORECASE,
)


def is_consequential_label(label: str) -> bool:
    return bool(CONSEQUENTIAL_RE.search(label or ""))


def _coerce_ref(args: dict[str, Any]) -> dict[str, Any]:
    if "ref" in args and args["ref"] is not None:
        args = dict(args)
        args["ref"] = int(args["ref"])
    return args


async def dispatch_tool(
    session: BrowserSession,
    name: str,
    raw_args: dict[str, Any],
    *,
    confirmed: bool = False,
) -> ToolResult:
    if name not in ALLOWED_TOOLS:
        obs = await session.snapshot()
        obs.error = f"Unknown tool '{name}'. Use one of: {', '.join(sorted(ALLOWED_TOOLS))}"
        return ToolResult(kind="observation", observation=obs)

    try:
        return await _dispatch_validated(session, name, raw_args or {}, confirmed=confirmed)
    except (ValidationError, ValueError, TypeError) as exc:
        obs = await session.snapshot()
        obs.error = f"Invalid arguments for {name}: {exc}"
        return ToolResult(kind="observation", observation=obs)


async def _dispatch_validated(
    session: BrowserSession,
    name: str,
    raw_args: dict[str, Any],
    *,
    confirmed: bool,
) -> ToolResult:
    if name == "navigate":
        args = NavigateArgs.model_validate(raw_args)
        return ToolResult(kind="observation", observation=await session.navigate(args.url))

    if name == "snapshot":
        return ToolResult(kind="observation", observation=await session.snapshot())

    if name == "click":
        args = RefArgs.model_validate(_coerce_ref(raw_args))
        _require_ref(session, args.ref)
        info = session.ref_info(args.ref)
        label = " ".join(filter(None, [info.name if info else "", info.role if info else "", info.input_type if info else ""]))
        if not confirmed and is_consequential_label(label):
            return ToolResult(
                kind="pause",
                pause_status=TaskStatus.needs_confirmation,
                pause_payload={
                    "summary": f'Click [{args.ref}] "{info.name if info else ""}" on {session.current_url()}',
                    "action": "click",
                    "ref": args.ref,
                    "pending_tool": "click",
                    "pending_args": {"ref": args.ref},
                },
            )
        return ToolResult(kind="observation", observation=await session.click(args.ref))

    if name == "type":
        args = TypeArgs.model_validate(_coerce_ref(raw_args))
        _require_ref(session, args.ref)
        return ToolResult(kind="observation", observation=await session.type_text(args.ref, args.text))

    if name == "select":
        args = SelectArgs.model_validate(_coerce_ref(raw_args))
        _require_ref(session, args.ref)
        return ToolResult(kind="observation", observation=await session.select(args.ref, args.value))

    if name == "press_key":
        args = PressKeyArgs.model_validate(raw_args)
        return ToolResult(kind="observation", observation=await session.press_key(args.key))

    if name == "ask_user":
        args = AskUserArgs.model_validate(raw_args)
        return ToolResult(
            kind="pause",
            pause_status=TaskStatus.needs_info,
            pause_payload={"question": args.question, "fields": args.fields},
        )

    if name == "request_confirmation":
        args = ConfirmationArgs.model_validate(_coerce_ref(raw_args) if raw_args.get("ref") else raw_args)
        pending_tool = "click"
        pending_args: dict[str, Any] = {}
        if args.ref is not None:
            _require_ref(session, args.ref)
            pending_args = {"ref": args.ref}
        return ToolResult(
            kind="pause",
            pause_status=TaskStatus.needs_confirmation,
            pause_payload={
                "summary": args.summary,
                "action": args.action,
                "ref": args.ref,
                "pending_tool": pending_tool if args.ref is not None else None,
                "pending_args": pending_args,
            },
        )

    if name == "request_human":
        args = HumanArgs.model_validate(raw_args)
        return ToolResult(
            kind="pause",
            pause_status=TaskStatus.needs_human,
            pause_payload={"reason": args.reason},
        )

    if name == "complete":
        args = CompleteArgs.model_validate(raw_args)
        return ToolResult(kind="complete", summary=args.summary, evidence=args.evidence)

    args = FailArgs.model_validate(raw_args)
    return ToolResult(kind="fail", reason=args.reason)


def _require_ref(session: BrowserSession, ref: int) -> None:
    if ref not in session.known_refs():
        known = ", ".join(str(n) for n in sorted(session.known_refs())) or "none"
        raise ValueError(f"ref [{ref}] is not in the current snapshot. Known refs: {known}")


def pending_from_pause(payload: dict[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    tool = payload.get("pending_tool")
    args = payload.get("pending_args") or {}
    if tool in BROWSER_TOOLS:
        return tool, args
    return None
