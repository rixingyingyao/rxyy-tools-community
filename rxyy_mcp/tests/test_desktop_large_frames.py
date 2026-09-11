"""Large Store snapshots are incoming data, separate from prompt size limits."""
import json
import struct
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_desktop as desktop


class DesktopFrameTests(unittest.TestCase):
    def pipe(self, wire):
        offset = [0]
        reads = []
        def read(_handle, count):
            data = wire[offset[0]:offset[0] + count]
            offset[0] += len(data)
            reads.append(count)
            return 0, data
        pipe = desktop._Pipe.__new__(desktop._Pipe)
        pipe.handle, pipe.buffer = 1, bytearray()
        pipe.file = SimpleNamespace(ReadFile=read)
        pipe.pipe = SimpleNamespace(PeekNamedPipe=lambda *_: (b'', len(wire) - offset[0], 0))
        return pipe, reads

    def test_65_mib_snapshot_then_next_frame_survive_chunked_reads(self):
        payload = b'{"text":"' + b'x' * (65 * 1024 * 1024) + b'"}'
        following = b'{"revision":2}'
        wire = struct.pack('<I', len(payload)) + payload + struct.pack('<I', len(following)) + following
        pipe, reads = self.pipe(wire)
        result = None
        deadline = time.monotonic() + 10
        while result is None and time.monotonic() < deadline:
            result = pipe.read(timeout=.01)
        self.assertEqual(65 * 1024 * 1024, len(result['text']))
        self.assertEqual({'revision': 2}, pipe.read())
        self.assertLessEqual(max(reads), 1024 * 1024)

    def test_unbounded_frame_is_rejected_before_reading_its_body(self):
        pipe, _ = self.pipe(b'')
        pipe.buffer.extend(struct.pack('<I', desktop.MAX_INCOMING_FRAME + 1))
        with self.assertRaisesRegex(desktop.DesktopFrameTooLarge, 'supported limit'):
            pipe.read()

    def test_outgoing_limit_does_not_expand_with_incoming_history_limit(self):
        pipe, _ = self.pipe(b'')
        with self.assertRaisesRegex(ValueError, 'request too large'):
            pipe.send({'text': 'x' * desktop.MAX_FRAME})


if __name__ == '__main__':
    unittest.main()
