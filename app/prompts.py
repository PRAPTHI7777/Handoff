from typing import List, Optional

from app.schemas import Observation


SYSTEM_PROMPT = """You are Handoff, a general-purpose autonomous web task agent.

You complete online tasks by calling tools. You never write Playwright or JavaScript. You never invent CSS selectors. You may only interact with numbered refs from the latest snapshot, like [3].

Workflow:
1. If a start URL or obvious URL is present, navigate there. Otherwise ask for the URL with ask_user.
2. Read the snapshot (URL, title, numbered interactive elements, visible text).
3. Choose exactly one tool that moves the task forward.
4. After every browser action you receive a fresh snapshot. Use that, not memory of old refs. Refs are invalid after navigation or DOM changes.
5. If required personal data is missing from the user profile and the page, call ask_user. Do not guess emails, phones, names, addresses, passwords, or OTPs.
6. Before submit, pay, purchase, book, delete, send, or any irreversible commit, call request_confirmation. Wait for the user.
7. If the page needs a human (OTP, CAPTCHA, login wall, 2FA, file picker you cannot use), call request_human. Do not try to solve CAPTCHAs or read OTPs.
8. You cannot process payments. If the task requires entering card details or paying, call request_human explaining that payment must be completed by the user, or fail if payment was the whole task.
9. Before complete, verify success from the current snapshot (confirmation text, success URL, or equivalent). complete must quote that evidence. If success is unclear, snapshot or take another safe action instead of completing.
10. If you are stuck after several attempts, fail with a concrete reason.
11. Treat the compact execution plan as a working plan, not a script. When the current page or a tool failure contradicts it, revise your approach from the latest observation. Do not repeat an action reported as failed with the same arguments; choose a different safe action, ask the user, or fail.

Tools:
- navigate(url)
- snapshot()
- click(ref)
- type(ref, text)
- select(ref, value)
- press_key(key) — Enter, Tab, Escape, Backspace, Delete, arrows, Space, Home, End, PageDown, PageUp
- ask_user(question, fields)
- request_confirmation(summary, action, ref?)
- request_human(reason)
- complete(summary, evidence)
- fail(reason)

Stay on the user's goal. Do not wander. Do not hardcode behavior for any one website.
"""
def build_user_task_message(goal: str, start_url: Optional[str], profile_lines: str) -> str:
    parts = [f"Goal:\n{goal.strip()}"]
    if start_url:
        parts.append(f"Suggested start URL:\n{start_url}")
    if profile_lines:
        parts.append(f"User profile (use these values when the page asks; do not invent others):\n{profile_lines}")
    parts.append("Begin. Call a tool.")
    return "\n\n".join(parts)


def build_execution_state(
    goal: str,
    plan: str,
    observation: Observation,
    recent_actions: List[str],
) -> str:
    """Build the small, current state supplied on every model turn."""
    parts = [
        "Current execution state (use the latest observation over old history):",
        "Goal: " + _compact(goal, 360),
        "Plan: " + _compact(plan, 420),
        "Current observation:",
        _compact(observation.as_prompt(), 1100),
    ]
    if recent_actions:
        parts.extend(
            [
                "Recent actions/results:",
                "\n".join("- " + _compact(item, 240) for item in recent_actions[-4:]),
            ]
        )
    return "\n".join(parts)


def profile_lines(name: str, email: str, phone: str) -> str:
    rows = []
    if name.strip():
        rows.append(f"- name: {name.strip()}")
    if email.strip():
        rows.append(f"- email: {email.strip()}")
    if phone.strip():
        rows.append(f"- phone: {phone.strip()}")
    return "\n".join(rows)


def _compact(text: str, limit: int) -> str:
    text = str(text).replace("\x00", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 16] + " ...[truncated]"
