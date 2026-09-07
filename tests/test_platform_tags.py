import importlib.util
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('platform_tags',
    Path(__file__).resolve().parents[1] / 'scripts' / 'publish_platform_tags.py')
tags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tags)


class PlatformTagsTests(unittest.TestCase):
    def manifest(self, arches=('arm64', 'amd64')):
        return {'mediaType': tags.DOCKER_LIST, 'manifests': [
            {'mediaType': tags.DOCKER_IMAGE, 'digest': 'sha256:' + arch,
             'platform': {'os': 'linux', 'architecture': arch}} for arch in arches]}

    def test_platform_selection_does_not_depend_on_order(self):
        self.assertEqual(tags.platform_digests(self.manifest())['arm64'], 'sha256:arm64')

    def test_reject_missing_duplicate_or_attestation_platforms(self):
        for arches in [('arm64',), ('arm64', 'arm64'), ('arm64', 'amd64', 'unknown')]:
            with self.subTest(arches=arches), self.assertRaises(ValueError):
                tags.platform_digests(self.manifest(arches))

    def test_publish_copies_children_without_overwriting_latest(self):
        child = {'mediaType': tags.DOCKER_IMAGE, 'config': {'digest': 'sha256:config'}}
        with mock.patch.object(tags, 'inspect', side_effect=[self.manifest()] + [child] * 6), \
                mock.patch.object(tags.subprocess, 'run') as run:
            tags.publish('owner/image', 'revision', '1.2.3')
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn('--prefer-index=false', call.args[0])
            self.assertNotIn('owner/image:latest', call.args[0])
            self.assertTrue(call.args[0][-1].startswith('owner/image@sha256:'))

    def test_readback_rejects_wrapped_index(self):
        child = {'mediaType': tags.DOCKER_IMAGE}
        with mock.patch.object(tags, 'inspect', side_effect=[self.manifest(), child, self.manifest()]), \
                mock.patch.object(tags.subprocess, 'run'), self.assertRaises(ValueError):
            tags.publish('owner/image', 'revision', '1.2.3')
