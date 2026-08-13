#!/usr/bin/env python3
"""Guard document naming rules until their table exists during fresh-site."""

from __future__ import annotations

import argparse
from pathlib import Path

FUNCTION = "def set_naming_from_document_naming_rule(doc):"
ANCHOR = "\tfrom frappe.model.base_document import DOCTYPES_FOR_DOCTYPE\n"
GUARD = (
    "\t# During fresh-site, Permission Type can create Custom Fields before the\n"
    "\t# Document Naming Rule DocType has been synchronized. No naming rules can\n"
    "\t# exist yet, so avoid querying a table that is not present.\n"
    "\tif not frappe.db.table_exists(\"Document Naming Rule\"):\n"
    "\t\treturn\n\n"
)


def patch_naming(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if text.count(FUNCTION) != 1:
        raise RuntimeError("unexpected Frappe naming source: function is not unique")
    start = text.index(FUNCTION)
    next_function = text.find("\ndef ", start + len(FUNCTION))
    block = text[start : next_function if next_function >= 0 else len(text)]
    if GUARD.strip() in block:
        return False
    if block.count(ANCHOR) != 1:
        raise RuntimeError("unexpected Frappe naming source: import anchor is not unique")
    block = block.replace(ANCHOR, ANCHOR + "\n" + GUARD, 1)
    text = text[:start] + block + text[next_function if next_function >= 0 else len(text) :]
    path.write_text(text, encoding="utf-8")
    final = path.read_text(encoding="utf-8")
    if final.count('if not frappe.db.table_exists("Document Naming Rule"):') != 1:
        raise RuntimeError("failed to install naming bootstrap guard exactly once")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print("frappe_naming_bootstrap_guard=" + ("applied" if patch_naming(args.source) else "already_present"))


if __name__ == "__main__":
    main()
