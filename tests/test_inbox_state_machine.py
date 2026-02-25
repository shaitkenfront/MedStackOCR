from __future__ import annotations

import unittest

from core.enums import DecisionStatus
from inbox.state_machine import (
    STATE_AWAIT_CONFIRM,
    STATE_AWAIT_FIELD_CANDIDATE,
    STATE_AWAIT_FIELD_SELECTION,
    STATE_AWAIT_FREE_TEXT,
    STATE_COMPLETED,
    STATE_IDLE,
    can_transition,
    initial_state_from_decision,
)


class InboxStateMachineTest(unittest.TestCase):
    def test_initial_state_from_decision(self) -> None:
        self.assertEqual(initial_state_from_decision(DecisionStatus.REVIEW_REQUIRED.value), STATE_AWAIT_CONFIRM)
        self.assertEqual(initial_state_from_decision(DecisionStatus.AUTO_ACCEPT.value), STATE_AWAIT_CONFIRM)
        self.assertEqual(initial_state_from_decision(DecisionStatus.REJECTED.value), STATE_IDLE)

    def test_can_transition(self) -> None:
        self.assertTrue(can_transition(STATE_AWAIT_CONFIRM, STATE_AWAIT_FIELD_SELECTION))
        self.assertTrue(can_transition(STATE_AWAIT_FIELD_SELECTION, STATE_AWAIT_FIELD_CANDIDATE))
        self.assertTrue(can_transition(STATE_AWAIT_FIELD_CANDIDATE, STATE_AWAIT_FREE_TEXT))
        self.assertFalse(can_transition(STATE_COMPLETED, STATE_AWAIT_CONFIRM))


if __name__ == "__main__":
    unittest.main()
