"""Isolated document extraction worker; input and output are bounded by parent."""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def main() -> None:
    metadata_line, content = sys.stdin.buffer.read().split(b"\n", 1)
    metadata = json.loads(metadata_line)
    decoded = content.decode(metadata["charset"], errors="strict")
    if metadata["media_type"] == "text/plain":
        title = "Untitled"
        text = decoded
    else:
        import trafilatura

        parser = _TitleParser()
        parser.feed(decoded)
        title = " ".join(" ".join(parser.parts).split())[:1_000] or "Untitled"
        text = trafilatura.extract(
            decoded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    if not text or len(text) > metadata["max_characters"]:
        raise ValueError("extracted content violates policy")
    sys.stdout.write(json.dumps({"title": title, "text": text}, separators=(",", ":")))


if __name__ == "__main__":
    main()
