"""Persist Bibit's embedding defaults in the effective Gateway configuration."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

import yaml


def update_config(path):
    path = Path(path).resolve(strict=True)
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict) or not isinstance(config.get('embed'), dict):
        raise ValueError('Gateway configuration must contain an embed mapping')
    args = config['embed'].get('default_args', [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError('embed.default_args must be a list of strings')
    overrides = {'--pooling': 'mean', '--embd-normalize': '2',
                 '--ctx-size': '2048', '--threads': '8', '--cache-ram': '0'}
    aliases = {'-c': '--ctx-size', '-t': '--threads'}
    result = []
    index = 0
    while index < len(args):
        option = args[index].split('=', 1)[0]
        if aliases.get(option, option) in overrides:
            if '=' not in args[index]:
                if index + 1 >= len(args) or args[index + 1].startswith('-'):
                    raise ValueError(f'Missing value for {option}')
                index += 1
        else:
            result.append(args[index])
        index += 1
    if '--embedding' not in result:
        result.append('--embedding')
    for option, value in overrides.items():
        result.extend([option, value])
    if args == result:
        return False
    config['embed']['default_args'] = result
    backup = path.with_name(path.name + '.pre-bibit-embedding')
    if not backup.exists():
        shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.bibit-embedding-')
    try:
        with os.fdopen(fd, 'w') as handle:
            yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
        stat = path.stat()
        os.chmod(temporary, stat.st_mode & 0o777)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    path = args.config
    if path is None:
        environment = subprocess.check_output([
            'systemctl', 'show', 'spacemit-ai-gateway.service',
            '--property=Environment', '--value'], text=True)
        values = dict(item.split('=', 1) for item in shlex.split(environment) if '=' in item)
        path = values.get('SPACEMIT_AI_GATEWAY_CONFIG')
        if not path:
            raise RuntimeError('Cannot determine effective Gateway configuration; refusing to guess')
    print('updated' if update_config(path) else 'unchanged')


if __name__ == '__main__':
    main()
