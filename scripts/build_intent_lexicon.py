"""Emit an apply_patch document for pinned WordNet lexical resources (no network)."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


ARCHIVE_SHA256 = '640db279c949a88f61f851dd54ebbb22d003f8b90b85267042ef85a3781d3a52'
SOURCE = 'https://wordnetcode.princeton.edu/3.0/WordNet-3.0.tar.gz'
DESTINATION = 'src/bibit_intent/resources/lexicons/'


def generate(archive_path):
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('WordNet archive checksum mismatch')
    with tarfile.open(archive_path) as archive:
        def read(name):
            return archive.extractfile('WordNet-3.0/' + name).read().decode('utf-8')

        verbs = sorted({line.split()[0] for line in read('dict/index.verb').splitlines()
                        if line and not line.startswith(' ')})
        exceptions = {parts[0]: parts[1:] for line in read('dict/verb.exc').splitlines()
                      if (parts := line.split())}
        data = {'source': SOURCE, 'archive_sha256': ARCHIVE_SHA256,
                'version': 'WordNet-3.0', 'verbs': verbs,
                'exceptions': dict(sorted(exceptions.items()))}
        return {'wordnet-verbs.json': json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                'WORDNET-LICENSE.txt': '\n'.join(line.rstrip() for line in read('COPYING').splitlines()) + '\n'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    print('*** Begin Patch')
    for name, content in generate(args.archive).items():
        path = DESTINATION + name
        if Path(path).exists():
            print('*** Delete File: ' + path)
        print('*** Add File: ' + path)
        print('\n'.join('+' + line for line in content.splitlines()))
    print('*** End Patch')


if __name__ == '__main__':
    main()
