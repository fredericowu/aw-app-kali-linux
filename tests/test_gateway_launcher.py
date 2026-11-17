"""Execute the s6 launcher across a stubbed process boundary, using pinned config.

CI checks out exactly the gateway ref from the image's Dockerfile. Locally set
AW_TEST_GATEWAY_CONFIG to that checkout's back/gateway/config.py. The shared
release workflow runs pytest without that checkout, so fetch the same pinned
source into process scratch when neither local source is available.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Runs in the Python process the real launcher execs, before any config import.
PROBE = '''
import builtins
import importlib.util
import json
import os
from pathlib import Path

foreign = Path(os.environ['FOREIGN_APPS']).resolve()
original_open = builtins.open

def guarded_open(path, *args, **kwargs):
    if isinstance(path, (str, os.PathLike)) and Path(path).resolve().is_relative_to(foreign):
        raise PermissionError('foreign config accessed: ' + str(path))
    return original_open(path, *args, **kwargs)

builtins.open = guarded_open
spec = importlib.util.spec_from_file_location('pinned_config', os.environ['AW_TEST_GATEWAY_CONFIG'])
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
assert config.scan_app_mcp_servers() == ({}, {})
assert config.scan_app_gateway_profiles() == ({}, {})
assert config.effective_named_configs() == ({}, {})
effective = config.effective_mcp_config(write_final=True)
assert effective['scanned'] == {'mcpServers': {}}
assert effective['final'] == json.loads(Path(os.environ['AW_MCP_CUSTOM_JSON']).read_text())
assert os.environ['AW_APP_SCAN_ROOTS'] == ''
assert os.environ['AW_HOST_MCP_JSON'] == ''
assert os.environ['DISPLAY'] == ':0'
assert os.environ['AW_MCP_GATEWAY_ALLOW'] == '*'
print(json.dumps({'env': {k: os.environ[k] for k in (
    'AW_APP_SCAN_ROOTS', 'AW_MCP_CUSTOM_JSON', 'AW_MCP_JSON', 'AW_GATEWAY_JSON')},
    'final': effective['final']}))
'''


@pytest.fixture(scope='session')
def gateway_config(tmp_path_factory):
    explicit = os.environ.get('AW_TEST_GATEWAY_CONFIG')
    path = Path(explicit) if explicit else ROOT / '.gateway-test/back/gateway/config.py'
    if explicit or path.is_file():
        assert path.is_file(), f'Missing pinned gateway config: {path}'
        return path
    ref = re.search(r'^ARG AW_MCP_GATEWAY_REF=(\S+)',
                    (ROOT / 'container/Dockerfile').read_text(), re.MULTILINE).group(1)
    url = f'https://raw.githubusercontent.com/tekflox/aw-mcp-gateway/{ref}/back/gateway/config.py'
    path = tmp_path_factory.mktemp('pinned-gateway') / 'config.py'
    with urllib.request.urlopen(url, timeout=30) as response:
        path.write_bytes(response.read())
    return path


@pytest.mark.parametrize('custom', [False, True])
@pytest.mark.parametrize('inherited', [False, True])
def test_launcher_isolates_mounted_workspace(tmp_path, custom, inherited, gateway_config):
    config_path = gateway_config
    foreign = tmp_path / 'workspace/apps'
    for app in ('readable', 'self-federation', 'unreadable'):
        directory = foreign / app
        directory.mkdir(parents=True)
        (directory / 'mcp.json').write_text(json.dumps({'mcpServers': {
            app: {'type': 'gateway', 'url': 'http://aw-app-kali-linux:9200/mcp'}}}))
        (directory / 'gateway-profiles.json').write_text(json.dumps({
            'profiles': {'foreign-profile': {'upstreams': [app]}}}))
    (foreign / 'unreadable/mcp.json').chmod(0)
    # The deterministic open trap above also works as root, unlike chmod alone.
    config_root = tmp_path / 'config'
    defaults = json.loads((ROOT / 'container/defaults/gateway-mcp.json').read_text())
    assert set(defaults['mcpServers']) == {'playwright', 'kali_control'}
    override = {'mcpServers': {'explicit-local': {'type': 'stdio', 'command': 'local'}}}
    gateway = tmp_path / 'gateway'
    (gateway / 'back').mkdir(parents=True)
    (gateway / '.venv/bin').mkdir(parents=True)
    probe = tmp_path / 'probe.py'
    probe.write_text(PROBE)
    python = gateway / '.venv/bin/python'
    python.write_text('#!/bin/sh\nexec "' + sys.executable + '" "' + str(probe) + '"\n')
    python.chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, content in {'chown': 'exit 0', 's6-setuidgid': 'shift\nexec "$@"'}.items():
        stub = bin_dir / name
        stub.write_text('#!/bin/sh\n' + content + '\n')
        stub.chmod(0o755)
    # Redirect only image filesystem paths; execute all launcher control flow.
    launcher = (ROOT / 'container/services/aw-mcp-gateway/run').read_text()
    launcher = launcher.replace('/config', str(config_root))
    launcher = launcher.replace('/defaults/aw-gateway-mcp.json', str(ROOT / 'container/defaults/gateway-mcp.json'))
    launcher = launcher.replace('/opt/aw-mcp-gateway', str(gateway))
    script = tmp_path / 'run'
    script.write_text(launcher)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               FOREIGN_APPS=str(foreign), AW_TEST_GATEWAY_CONFIG=str(config_path),
               AW_KALI_MCP_CONFIG=json.dumps(override) if custom else '', DISPLAY=':1',
               AW_HOST_MCP_JSON=str(tmp_path / 'host.json'))
    if inherited:
        env['AW_APP_SCAN_ROOTS'] = str(foreign)
    else:
        env.pop('AW_APP_SCAN_ROOTS', None)
    # Repeat boot with stale merged cache and a different previous custom pool.
    for boot in range(2):
        result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report['final'] == (override if custom else defaults)
        assert report['env'] == {
            'AW_APP_SCAN_ROOTS': '',
            'AW_MCP_CUSTOM_JSON': str(config_root / 'aw-mcp-gateway/mcp.custom.json'),
            'AW_MCP_JSON': str(config_root / 'aw-mcp-gateway/mcp.json'),
            'AW_GATEWAY_JSON': str(config_root / 'aw-mcp-gateway/gateway.json'),
        }
        (config_root / 'aw-mcp-gateway/mcp.json').write_text('{"mcpServers":{"stale":{}}}')
        (config_root / 'aw-mcp-gateway/mcp.custom.json').write_text('{"mcpServers":{"old-custom":{}}}')


def test_ci_gateway_ref_matches_image():
    image = (ROOT / 'container/Dockerfile').read_text()
    ref = re.search(r'^ARG AW_MCP_GATEWAY_REF=(\S+)', image, re.MULTILINE).group(1)
    workflow = (ROOT / '.github/workflows/test.yml').read_text()
    assert f'ref: {ref}' in workflow
