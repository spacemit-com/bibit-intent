"""Regression coverage for persistent Gateway embedding defaults."""
import importlib.util
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    'gateway_embedding_config',
    Path(__file__).resolve().parents[1] / 'deploy/configure-gateway-embedding.py',
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_persist_defaults_preserves_other_domains_and_is_idempotent(tmp_path):
    path = tmp_path / 'base.yaml'
    config = {'llm': {'default_args': ['--threads', '8']},
              'auth': {'enabled': False},
              'embed': {'storage': {'models_dir': '/models'}, 'default_args': [
                  '--embedding', '--metrics', '-t', '8', '-c', '32768',
                  '--pooling=last', '--cache-ram', '8192']}}
    original = yaml.safe_dump(config)
    path.write_text(original)
    assert module.update_config(path)
    updated = yaml.safe_load(path.read_text())
    assert updated['llm'] == config['llm']
    assert updated['auth'] == config['auth']
    assert updated['embed']['storage'] == config['embed']['storage']
    assert updated['embed']['default_args'] == [
        '--embedding', '--metrics', '--pooling', 'mean', '--embd-normalize', '2',
        '--ctx-size', '2048', '--threads', '8', '--cache-ram', '0']
    assert not module.update_config(path)
    assert path.with_name('base.yaml.pre-bibit-embedding').read_text() == original


@pytest.mark.parametrize('args', ['invalid', ['--threads'], ['--threads', '--metrics']])
def test_invalid_config_is_not_modified(tmp_path, args):
    path = tmp_path / 'base.yaml'
    original = yaml.safe_dump({'embed': {'default_args': args}})
    path.write_text(original)
    with pytest.raises(ValueError):
        module.update_config(path)
    assert path.read_text() == original
