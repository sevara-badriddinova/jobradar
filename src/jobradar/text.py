"""HTML -> plain text, using only the standard library."""

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "section", "article", "header", "footer", "blockquote",
}
_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(content: str | None) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text.

    Greenhouse returns job content HTML-escaped ("&lt;p&gt;..."), so we
    unescape once before parsing. Unescaping already-plain HTML is harmless.
    """
    if not content:
        return ""
    if "&lt;" in content:
        content = html.unescape(content)
    parser = _TextExtractor()
    parser.feed(content)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    # <li><p>item</p></li> (common in Ashby) leaves the bullet on its own line.
    text = re.sub(r"^- *\n+", "- ", text, flags=re.MULTILINE)
    # ...and a blank line between consecutive items; keep lists tight.
    text = re.sub(r"(^- .*)\n\n(?=- )", r"\1\n", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
