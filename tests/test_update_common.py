"""Signature and process-lock primitives used by the host and web gateway."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bundle_updater.common import FileLock, atomic_json, metadata, read_json, safe_path, sign, verify


class UpdateCommonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_signed_canonical_payload_cannot_be_changed_or_forged(self):
        token = 'fixture-private-' + 't' * 32
        payload = {'text': '更新', 'action': 'check', 'id': 'fixture'}
        envelope = sign(payload, token)
        self.assertEqual(verify(json.loads(json.dumps(envelope)), token), payload)
        self.assertEqual(envelope['signature'], sign(dict(reversed(list(payload.items()))), token)['signature'])
        envelope['payload']['action'] = 'apply'
        with self.assertRaises(ValueError):
            verify(envelope, token)
        for malformed in ({}, {'payload': {}, 'signature': '中文'}, {'payload': {}, 'signature': '0' * 64, 'extra': 1}):
            with self.assertRaises(ValueError):
                verify(malformed, token)

    def test_atomic_document_replaces_complete_file_and_bounds_reads(self):
        path = self.root / 'state.json'
        atomic_json(path, {'complete': 1})
        atomic_json(path, {'complete': 2})
        self.assertEqual(read_json(path), {'complete': 2})
        self.assertEqual(list(self.root.glob('.update-*')), [])
        with self.assertRaises(ValueError):
            read_json(path, 2)

    def test_hardlink_document_cannot_be_read_or_overwritten(self):
        original, linked = self.root / 'original.json', self.root / 'linked.json'
        atomic_json(original, {'private': True})
        try:
            os.link(original, linked)
        except OSError:
            self.skipTest('Hard links unavailable')
        with self.assertRaises(ValueError):
            read_json(linked)
        with self.assertRaises(ValueError):
            atomic_json(linked, {})
        self.assertEqual(json.loads(original.read_text()), {'private': True})

    def test_linked_ancestor_is_rejected(self):
        original, linked = self.root / 'original', self.root / 'linked'
        original.mkdir()
        try:
            linked.symlink_to(original, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError):
            safe_path(linked / 'new.json')

    def test_lock_excludes_other_process_and_releases_on_exit(self):
        path = self.root / 'daemon.lock'
        script = "from pathlib import Path; from bundle_updater.common import FileLock; import sys\ntry:\n with FileLock(Path(sys.argv[1])): pass\nexcept BlockingIOError:\n sys.exit(7)\n"
        command = [sys.executable, '-B', '-c', script, str(path)]
        with FileLock(path):
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
        result = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_metadata_requires_protocol_and_exact_three_components(self):
        value = {'format': 1, 'version': '0.6.0-alpha', 'update_protocol': 1,
                 'components': {'astrbot': '4.25.1', 'qwenpaw': '2.2.1', 'napcat': '4.18.33'}}
        atomic_json(self.root / 'bundle-version.json', value)
        self.assertEqual(metadata(self.root), value)
        value['components']['other'] = '1'
        atomic_json(self.root / 'bundle-version.json', value)
        with self.assertRaises(ValueError):
            metadata(self.root)
