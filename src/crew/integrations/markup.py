"""Small converters between Markdown and Confluence storage format (XHTML).

Covers what CREW reads and writes: headings, paragraphs, lists, fenced code,
inline code, pipe tables and links. Not a general-purpose converter.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    return _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', out)


def markdown_to_storage(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            code = "\n".join(body).replace("]]>", "]]]]><![CDATA[>")
            param = (
                f'<ac:parameter ac:name="language">{html.escape(lang)}</ac:parameter>'
                if lang
                else ""
            )
            out.append(
                f'<ac:structured-macro ac:name="code">{param}'
                f"<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body></ac:structured-macro>"
            )
            i += 1
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                head = "".join(f"<th>{_inline(c)}</th>" for c in rows[0])
                body_rows = "".join(
                    "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                    for r in rows[1:]
                )
                out.append(f"<table><tbody><tr>{head}</tr>{body_rows}</tbody></table>")
            continue
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(f"<li>{_inline(re.sub(r'^\s*[-*]\s+', '', lines[i]))}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if not stripped:
            i += 1
            continue
        para = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^(#|```|\||[-*]\s)", lines[i].strip())
        ):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br/>".join(_inline(p) for p in para) + "</p>")
    return "".join(out)


class _StorageToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_code = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("p", "table", "ul", "ol"):
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "tr":
            self.parts.append("\n| ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "ac:plain-text-body":
            self._in_code = True
            self.parts.append("\n\n```\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "ac:plain-text-body":
            self._in_code = False
            self.parts.append("\n```\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def unknown_decl(self, data: str) -> None:  # CDATA inside code macros
        if data.startswith("CDATA["):
            self.parts.append(data[len("CDATA[") :])


def storage_to_text(storage: str) -> str:
    parser = _StorageToText()
    parser.feed(storage)
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
