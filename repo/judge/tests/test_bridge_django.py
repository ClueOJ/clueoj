import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from judge.bridge.judge_handler import JudgeHandler
from judge.judgeapi import _recv_exact


class JudgeHandlerTestCase(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(JudgeHandler)
        handler.get_related_submission_data = Mock(return_value=SimpleNamespace(
            file_only=False,
            time=1,
            memory=64,
            short_circuit=False,
            pretests_only=False,
            contest_no=False,
            attempt_no=1,
            user_id=1,
            file_size_limit=0,
        ))
        handler.send = Mock()
        handler.close = Mock()
        handler._working = False
        handler._no_response_job = None
        handler.name = 'test-judge'
        return handler

    @patch('judge.bridge.judge_handler.threading.Timer')
    def test_submit_starts_acknowledgement_watchdog(self, timer):
        handler = self.make_handler()

        handler.submit(42, 'problem', 'PY3', 'source')

        self.assertEqual(timer.call_args[1]['args'], (42,))
        self.assertTrue(timer.return_value.daemon)
        timer.return_value.start.assert_called_once_with()
        self.assertEqual(handler._working, 42)

    @patch('judge.bridge.judge_handler.threading.Timer')
    def test_failed_send_rolls_back_assignment_and_watchdog(self, timer):
        handler = self.make_handler()
        handler.send.side_effect = OSError('socket closed')

        with self.assertRaises(OSError):
            handler.submit(42, 'problem', 'PY3', 'source')

        timer.return_value.cancel.assert_called_once_with()
        timer.return_value.start.assert_called_once_with()
        self.assertFalse(handler._working)
        self.assertIsNone(handler._no_response_job)

    def test_wrong_acknowledgement_stops_processing(self):
        handler = self.make_handler()
        handler._working = 42
        handler.on_submission_wrong_acknowledge = Mock()
        handler.on_submission_processing = Mock()

        with self.assertLogs('judge.bridge', level='ERROR'):
            handler.on_submission_acknowledged({'submission-id': 99})

        handler.close.assert_called_once_with()
        handler.on_submission_processing.assert_not_called()


class ReceiveExactTestCase(unittest.TestCase):
    def test_recv_exact_joins_partial_reads(self):
        sock = Mock()
        sock.recv.side_effect = [b'ab', b'cd']

        self.assertEqual(_recv_exact(sock, 4), b'abcd')

    def test_recv_exact_rejects_early_eof(self):
        sock = Mock()
        sock.recv.side_effect = [b'ab', b'']

        with self.assertRaises(ValueError):
            _recv_exact(sock, 4)
