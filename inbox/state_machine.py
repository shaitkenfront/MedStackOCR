from __future__ import annotations

from core.enums import DecisionStatus

STATE_IDLE = "IDLE"
STATE_AWAIT_CONFIRM = "AWAIT_CONFIRM"
STATE_AWAIT_FIELD_SELECTION = "AWAIT_FIELD_SELECTION"
STATE_AWAIT_FIELD_CANDIDATE = "AWAIT_FIELD_CANDIDATE"
STATE_AWAIT_FREE_TEXT = "AWAIT_FREE_TEXT"
STATE_HOLD = "HOLD"
STATE_COMPLETED = "COMPLETED"


def initial_state_from_decision(decision_status: str) -> str:
    if decision_status == DecisionStatus.REVIEW_REQUIRED.value:
        return STATE_AWAIT_CONFIRM
    if decision_status == DecisionStatus.AUTO_ACCEPT.value:
        return STATE_AWAIT_CONFIRM
    return STATE_IDLE


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True

    allowed: dict[str, set[str]] = {
        STATE_IDLE: {STATE_AWAIT_CONFIRM, STATE_COMPLETED, STATE_HOLD},
        STATE_AWAIT_CONFIRM: {STATE_AWAIT_FIELD_SELECTION, STATE_COMPLETED, STATE_HOLD},
        STATE_AWAIT_FIELD_SELECTION: {STATE_AWAIT_FIELD_CANDIDATE, STATE_AWAIT_CONFIRM, STATE_HOLD},
        STATE_AWAIT_FIELD_CANDIDATE: {STATE_AWAIT_FREE_TEXT, STATE_AWAIT_CONFIRM, STATE_AWAIT_FIELD_SELECTION, STATE_HOLD},
        STATE_AWAIT_FREE_TEXT: {STATE_AWAIT_CONFIRM, STATE_AWAIT_FIELD_SELECTION, STATE_HOLD},
        STATE_HOLD: {STATE_AWAIT_CONFIRM, STATE_COMPLETED},
        STATE_COMPLETED: set(),
    }
    return target in allowed.get(current, set())
