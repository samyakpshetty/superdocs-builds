"""The canonical edit-instruction contract, shared by the live and fake clients.

A reviewer change is expressed to SuperDocs as a natural-language instruction that also carries
machine-parseable delimiters. The live client sends the whole thing to the model, which reads it
as guidance; the fake client parses the delimiters to apply the *same* change deterministically.
Both honour the identical call shape, so the offline suite exercises the real code path — the fake
is not a shortcut around the contract, it is a faithful stand-in for it.

Security note: the instruction is always bound to one concrete passage (FIND) and one concrete
result (REPLACE_WITH). Reviewer text can never widen the scope to the whole document — a hostile
"ignore everything and rewrite the doc" comment still becomes exactly one scoped, gated change.
"""

from __future__ import annotations

from dataclasses import dataclass

from notion_review.domain import ChangeOperation

_OP = "<<<OP>>>"
_FIND = "<<<FIND>>>"
_REPLACE = "<<<REPLACE_WITH>>>"
_END = "<<<END>>>"


@dataclass(frozen=True)
class ParsedInstruction:
    operation: ChangeOperation
    find_text: str
    replace_text: str


def build_instruction(
    *,
    operation: ChangeOperation,
    find_text: str,
    replace_text: str,
    reviewer: str,
    comment: str = "",
) -> str:
    """Render a scoped edit instruction for SuperDocs."""
    lines = [
        "Apply one reviewer edit to this document. Change ONLY the passage marked FIND and "
        "leave every other part of the document exactly as it is.",
        "",
        f"Reviewer: {reviewer}",
    ]
    if comment:
        lines.append(f"Reviewer note: {comment}")
    lines += [
        "",
        _OP,
        operation.value,
        _FIND,
        find_text,
        _REPLACE,
        replace_text,
        _END,
    ]
    return "\n".join(lines)


def _between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i == -1:
        return None
    i += len(start)
    j = text.find(end, i)
    if j == -1:
        return None
    return text[i:j].strip("\n")


def parse_instruction(message: str) -> ParsedInstruction | None:
    """Reverse :func:`build_instruction`. Returns None if the message is not our contract."""
    op_raw = _between(message, _OP, _FIND)
    find_text = _between(message, _FIND, _REPLACE)
    replace_text = _between(message, _REPLACE, _END)
    if op_raw is None or find_text is None or replace_text is None:
        return None
    try:
        operation = ChangeOperation(op_raw.strip())
    except ValueError:
        return None
    return ParsedInstruction(operation=operation, find_text=find_text, replace_text=replace_text)
