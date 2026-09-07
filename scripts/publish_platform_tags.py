"""Publish optional single-manifest aliases from already verified build images."""
import json
import os
import subprocess


DOCKER_LIST = 'application/vnd.docker.distribution.manifest.list.v2+json'
DOCKER_IMAGE = 'application/vnd.docker.distribution.manifest.v2+json'


def inspect(reference):
    return json.loads(subprocess.check_output(
        ['docker', 'buildx', 'imagetools', 'inspect', reference, '--raw'], text=True))


def platform_digests(manifest):
    if manifest.get('mediaType') != DOCKER_LIST:
        raise ValueError('Expected verified Docker manifest list')
    result = {}
    for entry in manifest.get('manifests', []):
        platform = entry.get('platform', {})
        arch = platform.get('architecture')
        if (platform.get('os') != 'linux' or arch not in ('amd64', 'arm64')
                or arch in result or entry.get('mediaType') != DOCKER_IMAGE):
            raise ValueError('Unexpected or duplicate image platform')
        result[arch] = entry['digest']
    if set(result) != {'amd64', 'arm64'}:
        raise ValueError('Both amd64 and arm64 are required')
    return result


def publish(image, revision, version):
    for arch, digest in platform_digests(inspect(f'{image}:sha-{revision}')).items():
        source = f'{image}@{digest}'
        expected = inspect(source)
        if expected.get('mediaType') != DOCKER_IMAGE or 'manifests' in expected:
            raise ValueError('Source must be a single Docker image manifest')
        # A single-platform build can still be wrapped in an index. Explicitly
        # copy its child manifest instead; never change the existing latest tag.
        tags = [f'{image}:{version}-{arch}', f'{image}:latest-{arch}']
        subprocess.run(['docker', 'buildx', 'imagetools', 'create',
                        '--prefer-index=false', '-t', tags[0], '-t', tags[1], source], check=True)
        for tag in tags:
            if inspect(tag) != expected:
                raise ValueError(f'{tag} does not match its source manifest')
            print(f'Verified {tag}: single Docker manifest, source={digest}', flush=True)


if __name__ == '__main__':
    for variant in ('lite', 'web'):
        publish(f"{os.environ['DOCKER_USERNAME']}/aircat-server-{variant}",
                os.environ['IMAGE_REVISION'], os.environ['IMAGE_VERSION'])
