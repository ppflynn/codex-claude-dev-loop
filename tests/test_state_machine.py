import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.state_machine import Status, StateTransitionError, cancel, transition


class StateMachineTests(unittest.TestCase):
    def test_happy_path_transitions(self):
        status = transition(Status.CREATED, Status.WAITING_FOR_CLAUDE)
        status = transition(status, Status.CLAUDE_WINDOW_STARTED)
        status = transition(status, Status.WAITING_FOR_CODEX)
        status = transition(status, Status.CODEX_WINDOW_STARTED)
        self.assertEqual(transition(status, Status.PASS), Status.PASS)

    def test_needs_fix_returns_to_claude(self):
        status = transition(Status.CODEX_WINDOW_STARTED, Status.NEEDS_FIX)
        self.assertEqual(transition(status, Status.WAITING_FOR_CLAUDE), Status.WAITING_FOR_CLAUDE)

    def test_max_rounds_can_fail_after_needs_fix(self):
        status = transition(Status.CODEX_WINDOW_STARTED, Status.NEEDS_FIX)
        self.assertEqual(transition(status, Status.FAILED), Status.FAILED)

    def test_terminal_status_cannot_continue(self):
        with self.assertRaises(StateTransitionError):
            transition(Status.PASS, Status.WAITING_FOR_CLAUDE)

    def test_illegal_transition_raises_clear_exception(self):
        with self.assertRaisesRegex(StateTransitionError, "Invalid task status transition"):
            transition(Status.WAITING_FOR_CLAUDE, Status.WAITING_FOR_CODEX)

    def test_non_terminal_can_cancel(self):
        self.assertEqual(cancel(Status.WAITING_FOR_CODEX), Status.CANCELLED)


if __name__ == "__main__":
    unittest.main()
