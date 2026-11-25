import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import serve_mode


class ServeModeTest(unittest.TestCase):
    def test_mode(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PYLON2_DAEMON_MODE', None)
            self.assertEqual(serve_mode.mode({}), 'passive')
            self.assertEqual(serve_mode.mode({'mode': 'server'}), 'server')
            with patch.dict(os.environ, {'PYLON2_DAEMON_MODE': 'passive'}):
                self.assertEqual(serve_mode.mode({'mode': 'server'}), 'passive')     # env wins
            with self.assertRaises(SystemExit):
                serve_mode.mode({'mode': 'bogus'})

    def test_passive_url_and_token(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False):
            for k in ('PYLON2_DAEMON_MODE', 'PYLON2_FRAMEWORK_URL', 'PYLON2_AGENT_TOKEN'):
                os.environ.pop(k, None)
            tok = Path(d) / 'agent-token'
            tok.write_text('file-token\n')
            url, headers, server = serve_mode.setup({'framework_url': 'wss://h/x/agent', 'agent_token_file': str(tok)},
                                                    'ws://default/agent')
            self.assertEqual((url, headers, server), ('wss://h/x/agent', {'Authorization': 'Bearer file-token'}, None))
            with patch.dict(os.environ, {'PYLON2_AGENT_TOKEN': 'env-token', 'PYLON2_FRAMEWORK_URL': 'ws://e/agent'}):
                url, headers, _ = serve_mode.setup({'agent_token_file': str(tok)}, 'ws://default/agent')
                self.assertEqual((url, headers), ('ws://e/agent', {'Authorization': 'Bearer env-token'}))
            url, headers, _ = serve_mode.setup({'agent_token_file': str(Path(d) / 'missing')}, 'ws://default/agent')
            self.assertEqual((url, headers), ('ws://default/agent', {}))

    def test_ws_connect_header_argument_across_versions(self):
        calls = []

        def new_api(url, additional_headers=None, **kw):
            calls.append(('additional', additional_headers))

        def legacy_api(url, extra_headers=None, **kw):       # like websockets<=13: swallows unknown kwargs
            calls.append(('extra', extra_headers))
        serve_mode.ws_connect(new_api, 'ws://x', {'A': '1'})
        serve_mode.ws_connect(legacy_api, 'ws://x', {'A': '1'})
        serve_mode.ws_connect(legacy_api, 'ws://x', {})
        self.assertEqual(calls, [('additional', {'A': '1'}), ('extra', {'A': '1'}), ('extra', None)])

    def test_explicit_release_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):serve_mode.LocalServer({})
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):serve_mode.LocalServer({'server_root':tmp})
                root=Path(tmp);(root/'main.py').touch();(root/'composition.json').write_text('{}')
                server=serve_mode.LocalServer({'server_root':tmp})
                with patch.object(serve_mode.subprocess,'Popen') as spawn:
                    server._spawn()
                    self.assertEqual(spawn.call_args.kwargs['cwd'],str(root.resolve()))
                    self.assertEqual(spawn.call_args.args[0][0],str(Path(serve_mode.sys.executable).resolve()))

    def test_server_env_carries_only_the_token_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'main.py').touch(); (root/'composition.json').write_text('{}')
            s = serve_mode.LocalServer({'server_root':tmp,'server_port': 1, 'server_env': {'X': 1}})
        env = s._env()
        self.assertNotIn(s.token, ''.join(env.values()))
        self.assertEqual(len(env['PYLON_AGENT_TOKEN_SHA256']), 64)
        self.assertEqual(env['X'], '1')


if __name__ == '__main__':
    unittest.main()
