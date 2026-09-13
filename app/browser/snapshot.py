from typing import Any

from playwright.async_api import Page

from app.schemas import Observation, RefInfo

COLLECT_JS = """
() => {
  document.querySelectorAll("[data-handoff-ref]").forEach((el) => {
    el.removeAttribute("data-handoff-ref");
  });

  const selector = [
    "a[href]",
    "button",
    "input",
    "textarea",
    "select",
    "[role='button']",
    "[role='link']",
    "[role='checkbox']",
    "[role='radio']",
    "[role='textbox']",
    "[role='combobox']",
    "[role='menuitem']",
    "[role='tab']",
    "[contenteditable='true']",
  ].join(",");

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width >= 2 && rect.height >= 2;
  };

  const accessibleName = (el) => {
    const aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return (lab.innerText || "").trim();
    }
    const wrapped = el.closest("label");
    if (wrapped) {
      const clone = wrapped.cloneNode(true);
      clone.querySelectorAll("input,select,textarea,button").forEach((n) => n.remove());
      const t = (clone.innerText || "").trim();
      if (t) return t;
    }
    return (
      (el.getAttribute("placeholder") || "").trim() ||
      (el.getAttribute("name") || "").trim() ||
      (el.getAttribute("title") || "").trim() ||
      (el.getAttribute("value") || "").trim() ||
      (el.innerText || "").trim()
    );
  };

  const nodes = Array.from(document.querySelectorAll(selector)).filter((el) => {
    if (el.disabled) return false;
    return visible(el);
  });

  const items = [];
  let ref = 1;
  for (const el of nodes) {
    if (ref > 80) break;
    el.setAttribute("data-handoff-ref", String(ref));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    let value = "";
    if ("value" in el && type !== "password") {
      value = String(el.value || "").slice(0, 80);
    }
    items.push({
      ref,
      tag,
      role: (el.getAttribute("role") || tag).toLowerCase(),
      name: accessibleName(el).replace(/\\s+/g, " ").slice(0, 120),
      input_type: type,
      value,
    });
    ref += 1;
  }

  const pageText = (document.body && document.body.innerText ? document.body.innerText : "")
    .replace(/\\s+/g, " ")
    .trim()
    .slice(0, 1500);

  return {
    url: location.href,
    title: document.title || "",
    items,
    pageText,
  };
}
"""


def _format_refs(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        extra = ""
        if item.get("input_type"):
            extra += f" type={item['input_type']}"
        if item.get("value"):
            extra += f' value="{item["value"]}"'
        name = item.get("name") or "(unnamed)"
        lines.append(f'[{item["ref"]}] {item.get("role") or item.get("tag")} "{name}"{extra}')
    return "\n".join(lines) if lines else "(no interactive elements found)"


async def capture_snapshot(page: Page) -> tuple[Observation, dict[int, RefInfo]]:
    data = await page.evaluate(COLLECT_JS)
    refs: dict[int, RefInfo] = {}
    for raw in data.get("items") or []:
        info = RefInfo(
            ref=int(raw["ref"]),
            tag=raw.get("tag") or "",
            role=raw.get("role") or "",
            name=raw.get("name") or "",
            input_type=raw.get("input_type") or "",
            value=raw.get("value") or "",
        )
        refs[info.ref] = info
    observation = Observation(
        url=data.get("url") or page.url,
        title=data.get("title") or "",
        refs_text=_format_refs(data.get("items") or []),
        page_text=data.get("pageText") or "",
    )
    return observation, refs
