"""The inbound review as a durable, human-gated LangGraph.

Three nodes — propose, gate, apply — with a checkpointer so the run survives a crash or a
days-long pause, and a real ``interrupt`` at the gate so the human approves changes item by item.
The graph holds only ids and small serialisable data; the review round itself lives in the Store,
which is the source of truth. Nodes close over the service clients, so the same graph runs against
the fakes (keyless tests) and the live services.

Path-changing decisions live in ``_route_after_propose``: nothing to review, or a parked run
(budget/quota), skips the gate — the graph adapts instead of marching through empty stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from notion_review.config import Config
from notion_review.docx_markup import parse_docx
from notion_review.domain import ProposedChange, ReviewRound, RoundStatus
from notion_review.logging import get_logger
from notion_review.notion.base import NotionClient
from notion_review.roundtrip.inbound import (
    MatchedEdit,
    apply_decisions,
    match_edits,
    propose_changes,
)
from notion_review.store import Store
from notion_review.superdocs.base import SuperDocsClient

_log = get_logger("notion_review.graph")


class ReviewState(TypedDict, total=False):
    round_id: str
    matched: list[dict[str, Any]]
    decisions: list[dict[str, Any]]


@dataclass
class ReviewGate:
    """The paused state handed to the human: the round and the changes awaiting approval."""

    round: ReviewRound
    pending: list[ProposedChange]


def _load(store: Store, round_id: str) -> ReviewRound:
    round_ = store.get(round_id)
    if round_ is None:
        raise KeyError(f"review round not found: {round_id}")
    return round_


def build_review_graph(
    *,
    notion: NotionClient,
    superdocs: SuperDocsClient,
    store: Store,
    config: Config,
    checkpointer: Any,
) -> Any:
    """Compile the propose → gate → apply graph with its dependencies bound in."""

    def propose(state: ReviewState) -> ReviewState:
        round_ = _load(store, state["round_id"])
        round_.status = RoundStatus.INGESTING
        edits = [MatchedEdit.model_validate(m) for m in state.get("matched", [])]
        propose_changes(round_, superdocs, edits, config)
        if round_.status != RoundStatus.PARKED and round_.pending():
            round_.status = RoundStatus.AWAITING_APPROVAL
        store.save(round_)
        _log.info(
            "changes_proposed",
            extra={
                "round_id": round_.id,
                "pending": len(round_.pending()),
                "ops": round_.ops_spent,
            },
        )
        return {}

    def gate(state: ReviewState) -> ReviewState:
        round_ = _load(store, state["round_id"])
        payload = {
            "round_id": round_.id,
            "pending": [p.model_dump(mode="json") for p in round_.pending()],
        }
        decisions = interrupt(payload)  # pause here until a human resumes with decisions
        return {"decisions": list(decisions)}

    def apply(state: ReviewState) -> ReviewState:
        round_ = _load(store, state["round_id"])
        apply_decisions(round_, state.get("decisions", []), notion)
        store.save(round_)
        _log.info(
            "changes_applied",
            extra={"round_id": round_.id, "status": round_.status.value},
        )
        return {}

    def finalize(state: ReviewState) -> ReviewState:
        round_ = _load(store, state["round_id"])
        if round_.status not in (RoundStatus.PARKED, RoundStatus.FAILED):
            round_.status = RoundStatus.COMPLETED
        store.save(round_)
        return {}

    def route_after_propose(state: ReviewState) -> str:
        round_ = _load(store, state["round_id"])
        if round_.status == RoundStatus.PARKED:
            return "finalize"
        return "gate" if round_.pending() else "finalize"

    graph = StateGraph(ReviewState)
    graph.add_node("propose", propose)
    graph.add_node("gate", gate)
    graph.add_node("apply", apply)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "propose")
    graph.add_conditional_edges(
        "propose", route_after_propose, {"gate": "gate", "finalize": "finalize"}
    )
    graph.add_edge("gate", "apply")
    graph.add_edge("apply", END)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


class InboundController:
    """Drives the inbound graph: start a review to the gate, then submit the human's decisions."""

    def __init__(
        self,
        *,
        notion: NotionClient,
        superdocs: SuperDocsClient,
        store: Store,
        config: Config,
        checkpointer: Any | None = None,
    ) -> None:
        self._store = store
        self._graph = build_review_graph(
            notion=notion,
            superdocs=superdocs,
            store=store,
            config=config,
            checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        )

    def start(self, *, round_id: str, docx_bytes: bytes) -> ReviewGate:
        """Parse the returned markup, propose each change, and pause at the approval gate."""
        round_ = _load(self._store, round_id)
        markup = parse_docx(docx_bytes)
        matched, unmatched = match_edits(markup, round_.block_map)
        if unmatched:
            _log.warning("unmatched_changes", extra={"round_id": round_id, "count": len(unmatched)})
        state: ReviewState = {
            "round_id": round_id,
            "matched": [edit.model_dump() for edit in matched],
        }
        self._graph.invoke(state, self._config(round_id))
        round_ = _load(self._store, round_id)
        return ReviewGate(round=round_, pending=round_.pending())

    def submit(self, *, round_id: str, decisions: list[dict[str, Any]]) -> ReviewRound:
        """Resume the paused graph with the human's decisions and apply the approved changes."""
        self._graph.invoke(Command(resume=decisions), self._config(round_id))
        return _load(self._store, round_id)

    def _config(self, round_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": round_id}}
