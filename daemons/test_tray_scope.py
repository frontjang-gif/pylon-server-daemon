"""The Windows tray helper's singleton sweep only touches helpers of the SAME install."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent / 'windows' / 'caps'


def load_host(port_file, name=None):
    env = {'PYLON2_TRAY_PORT_FILE': port_file}
    if name:
        env['PYLON2_TRAY_NAME'] = name
    with patch.dict(os.environ, env):
        sys.path.insert(0, str(HERE))
        try:
            spec = importlib.util.spec_from_file_location('tray_host_under_test', str(HERE / '_tray_host.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            sys.path.remove(str(HERE))


class TrayScopeTest(unittest.TestCase):
    def test_sidecar_files_follow_the_install(self):
        with tempfile.TemporaryDirectory() as d:
            mod = load_host(os.path.join(d, 'pylon-server-tray-8910.port'))
            self.assertEqual(Path(mod._PID_FILE).name, 'pylon-server-tray-8910.pid')
            self.assertEqual(Path(mod._STATE_FILE).name, 'pylon-server-tray-8910.state')

    def test_tray_is_branded_for_its_install(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PYLON2_TRAY_NAME', None)
            self.assertEqual(load_host(os.path.join(d, 'a.port')).NAME, 'ai-pylon2')
            self.assertEqual(load_host(os.path.join(d, 'b.port'), 'pylon-server').NAME, 'pylon-server')
        src = (HERE / '_tray_host.py').read_text()
        self.assertNotIn('MenuItem("ai-pylon2"', src)
        self.assertNotIn('"Open dashboard"', src)

    def test_sweep_kills_only_same_install(self):
        with tempfile.TemporaryDirectory() as d:
            mod = load_host(os.path.join(d, 'pylon-server-tray-8910.port'))
            wmic = '\n'.join([
                'Node,CommandLine,ProcessId',
                r'PC,C:\py\python.exe C:\ai-pylon2\daemons\windows\caps\_tray_host.py http://127.0.0.1:8900,111',
                r'PC,C:\py\python.exe C:\pylon-server\daemons\windows\caps\_tray_host.py http://127.0.0.1:8910,222',
                r'PC,C:\py\python.exe C:\other\_tray_host.py http://127.0.0.1:89100,333',
                r'PC,C:\py\python.exe C:\pylon-server\daemons\windows\caps\_tray_host.py http://127.0.0.1:8910,%d' % os.getpid(),
            ])
            killed = []

            class R:
                stdout = wmic
            with patch.object(mod, '_terminate', side_effect=killed.append), \
                    patch('subprocess.run', return_value=R()):
                mod._kill_other_tray_hosts('http://127.0.0.1:8910')
            self.assertEqual(killed, [222])            # never 111 (ai-pylon2), 333, or ourselves


if __name__ == '__main__':
    unittest.main()
