import importlib.util,sys,types,tempfile,os,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'caps'
PKG='windows_mode_fixture'
pkg=types.ModuleType(PKG);pkg.__path__=[str(ROOT)];sys.modules[PKG]=pkg
spec=importlib.util.spec_from_file_location(PKG+'.system_cdp',ROOT/'system_cdp.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
class Tests(unittest.TestCase):
 def test_switch_and_protect_tabs(self):
  with tempfile.TemporaryDirectory() as tmp:
   port=Path(tmp)/'edge.port';port.write_text('9222')
   edge=types.ModuleType(PKG+'._edge_profile');edge.PORT_FILE=str(port)
   edge.current_mode=Mock(side_effect=['headless','headed'])
   edge.prefer_headed=Mock(return_value=False)
   edge.ensure_mode=Mock(return_value='http://127.0.0.1:9223')
   c=Mock();c.DEBUG_URL='http://127.0.0.1:9222';c.page_tabs.return_value=[{'url':'https://example.invalid'}]
   c.open_tab.return_value={'id':'fixture','webSocketDebuggerUrl':'ws://127.0.0.1:9223/devtools/page/fixture'}
   a={'url':'https://console.cloud.google.com/auth/clients?project=example-project','browser_mode':'headful','confirm':True}
   with patch.dict(sys.modules,{PKG+'._edge_profile':edge}),patch.object(pkg,'_edge_profile',edge,create=True),patch.object(m,'cdp',c),patch.dict(os.environ,{'CDP_DEBUG_URL':''}):
    self.assertFalse(m.navigate_mode(a,{'user':'fixture'})['ok']);edge.ensure_mode.assert_not_called()
    edge.current_mode.side_effect=['headless','headed']
    r=m.navigate_mode({**a,'restart_managed':True},{'user':'fixture'})
    self.assertEqual(r,{'ok':True,'tab_id':'fixture','browser_mode':'headful'});edge.ensure_mode.assert_called_once_with(headless=False);c.ws_send_recv.assert_called_once()
    self.assertFalse(m.navigate_mode({**a,'url':'https://evil.invalid'},{'user':'fixture'})['ok'])
    with patch.dict(os.environ,{'CDP_DEBUG_URL':'http://127.0.0.1:1234'}):self.assertFalse(m.navigate_mode(a,{'user':'fixture'})['ok'])
