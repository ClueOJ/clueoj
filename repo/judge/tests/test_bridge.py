import unittest
from unittest.mock import Mock

from judge.bridge.base_handler import Disconnect, ZlibPacketHandler
from judge.bridge.judge_list import InvalidSubmission, JudgeList


class ZlibPacketHandlerTestCase(unittest.TestCase):
    def test_read_sized_packet_disconnects_on_eof_before_full_payload(self):
        handler = Mock()
        handler.request.recv.side_effect = [b'partial', b'']

        with self.assertRaises(Disconnect):
            ZlibPacketHandler.read_sized_packet(handler, 8)

        self.assertEqual(handler.request.recv.call_count, 2)
        handler._on_packet.assert_not_called()


class JudgeListTestCase(unittest.TestCase):
    def make_judge(self, name='judge', working=False):
        judge = Mock()
        judge.name = name
        judge.is_disabled = False
        judge._working = 1 if working else False
        judge.working = working
        judge.load = 0
        judge.can_judge.return_value = True
        judge.get_current_submission.side_effect = lambda: judge._working or None
        return judge

    def test_register_disconnects_existing_judge_with_same_name(self):
        judges = JudgeList()
        existing = self.make_judge(name='duplicate')
        replacement = self.make_judge(name='duplicate')
        judges.judges.add(existing)

        judges.register(replacement)

        existing.disconnect.assert_called_once_with(force=True)
        self.assertIn(replacement, judges.judges)

    def test_failed_dispatch_does_not_leave_stale_assignment(self):
        judges = JudgeList()
        judge = self.make_judge()
        judge.submit.side_effect = OSError('socket closed')
        judges.judges.add(judge)

        with self.assertLogs('judge.bridge', level='ERROR'):
            judges.judge(42, 'problem', 'PY3', 'source', None, 1)

        self.assertNotIn(42, judges.submission_map)
        self.assertIn(42, judges.node_map)
        judge.disconnect.assert_called_once_with(force=True)

    def test_wrong_completion_does_not_release_another_submission(self):
        judges = JudgeList()
        first = self.make_judge(name='first', working=True)
        second = self.make_judge(name='second')
        second._working = 2
        second.working = True
        judges.judges.update((first, second))
        judges.submission_map.update({1: first, 2: second})

        with self.assertLogs('judge.bridge', level='ERROR'):
            self.assertFalse(judges.on_judge_free(first, 2))

        self.assertIs(judges.submission_map[1], first)
        self.assertIs(judges.submission_map[2], second)
        self.assertEqual(first._working, 1)

    def test_vanished_submission_does_not_disconnect_judge(self):
        judges = JudgeList()
        judge = self.make_judge()
        judge.submit.side_effect = InvalidSubmission(42)
        judges.judges.add(judge)

        with self.assertLogs('judge.bridge', level='WARNING'):
            judges.judge(42, 'problem', 'PY3', 'source', None, 1)

        self.assertNotIn(42, judges.submission_map)
        self.assertNotIn(42, judges.node_map)
        self.assertIn(judge, judges.judges)
        judge.disconnect.assert_not_called()

    def test_reenabled_idle_judge_drains_queue(self):
        judges = JudgeList()
        judge = self.make_judge()
        judge.is_disabled = True
        judges.judges.add(judge)
        judges.judge(42, 'problem', 'PY3', 'source', None, 1)

        judges.update_disable_judge(judge.name, False)

        judge.submit.assert_called_once_with(42, 'problem', 'PY3', 'source')
        self.assertNotIn(42, judges.node_map)
        self.assertIs(judges.submission_map[42], judge)
