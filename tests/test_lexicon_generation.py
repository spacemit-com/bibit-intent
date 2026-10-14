"""Reproducible source extraction, with synthetic archives and no network."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[1] / 'scripts/build_intent_lexicon.py'
    spec = importlib.util.spec_from_file_location('build_intent_lexicon', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_modified_archive_is_rejected(builder, tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    archive.write_bytes(b'not the pinned archive')
    with pytest.raises(ValueError, match='checksum mismatch'):
        builder.generate(archive)


def test_generation_is_deterministic_and_preserves_notice(builder, tmp_path, monkeypatch):
    archive = tmp_path / 'fixture.tar.gz'
    entries = {
        'dict/index.verb': '  source header\nwrite v 1\nread v 1\nwrite v 1\n',
        'dict/verb.exc': 'wrote write\nread read\n',
        'COPYING': 'Fixture notice\nPermission and disclaimer\n',
    }
    with tarfile.open(archive, 'w:gz') as stream:
        for name, content in entries.items():
            payload = content.encode()
            member = tarfile.TarInfo('WordNet-3.0/' + name)
            member.size = len(payload)
            stream.addfile(member, io.BytesIO(payload))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(builder, 'ARCHIVE_SHA256', checksum)
    first = builder.generate(archive)
    assert first == builder.generate(archive)
    data = json.loads(first['wordnet-verbs.json'])
    assert data['verbs'] == ['read', 'write']
    assert data['exceptions']['wrote'] == ['write']
    assert data['archive_sha256'] == checksum
    assert first['WORDNET-LICENSE.txt'] == entries['COPYING']
    assert sorted(path.name for path in tmp_path.iterdir()) == ['fixture.tar.gz']
