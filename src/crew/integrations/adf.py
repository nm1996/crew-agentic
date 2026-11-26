"""Atlassian Document Format (Jira/Confluence rich text) <-> plain Markdown-ish text.

Only what CREW needs: readable text from issue descriptions, and simple
paragraph/list/code documents for comments.
"""

from __future__ import annotations

from typing import Any


def adf_to_text(node: Any) -> str:
    """Render an ADF document (or plain string) as Markdown-like text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return _render(node).strip()


def _children(node: dict[str, Any]) -> str:
    return "".join(_render(child) for child in node.get("content", []) or [])


def _render(node: dict[str, Any]) -> str:
    kind = node.get("type")
    if kind == "text":
        text = node.get("text", "")
        marks = {m.get("type") for m in node.get("marks", []) or []}
        if "code" in marks:
            text = f"`{text}`"
        for mark in node.get("marks", []) or []:
            if mark.get("type") == "link":
                text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "paragraph":
        return _children(node) + "\n\n"
    if kind == "heading":
        level = int(node.get("attrs", {}).get("level", 2))
        return "#" * level + " " + _children(node).strip() + "\n\n"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for i, item in enumerate(node.get("content", []) or [], 1):
            bullet = f"{i}." if kind == "orderedList" else "-"
            body = _children(item).strip().replace("\n\n", "\n").replace("\n", "\n  ")
            lines.append(f"{bullet} {body}")
        return "\n".join(lines) + "\n\n"
    if kind == "codeBlock":
        lang = node.get("attrs", {}).get("language") or ""
        return f"```{lang}\n{_children(node)}\n```\n\n"
    if kind == "blockquote":
        return "\n".join("> " + ln for ln in _children(node).strip().splitlines()) + "\n\n"
    if kind == "rule":
        return "---\n\n"
    if kind in ("mention", "emoji", "inlineCard"):
        attrs = node.get("attrs", {})
        return str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or "")
    return _children(node)


def text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text as an ADF document: paragraphs, ``- `` lists and fenced code."""
    content: list[dict[str, Any]] = []
    blocks = text.replace("\r\n", "\n").split("\n\n")
    in_code: list[str] | None = None
    for block in blocks:
        if in_code is not None:
            if block.rstrip().endswith("```"):
                in_code.append(block.rstrip()[:-3])
                content.append(_code("\n\n".join(in_code)))
                in_code = None
            else:
                in_code.append(block)
            continue
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            body = stripped[3:].split("\n", 1)[1] if "\n" in stripped else ""
            if stripped.endswith("```") and len(stripped) > 3:
                content.append(_code(body[:-3].rstrip("\n")))
            else:
                in_code = [body]
            continue
        lines = stripped.splitlines()
        if all(ln.lstrip().startswith(("- ", "* ")) for ln in lines):
            content.append(
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_paragraph(ln.lstrip()[2:])]}
                        for ln in lines
                    ],
                }
            )
        else:
            content.append(_paragraph(stripped))
    if in_code is not None:
        content.append(_code("\n\n".join(in_code)))
    return {"type": "doc", "version": 1, "content": content or [_paragraph("")]}


def _paragraph(text: str) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            parts.append({"type": "hardBreak"})
        if line:
            parts.append({"type": "text", "text": line})
    return {"type": "paragraph", "content": parts}


def _code(text: str) -> dict[str, Any]:
    return {"type": "codeBlock", "content": [{"type": "text", "text": text or " "}]}
