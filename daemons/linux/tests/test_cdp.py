import importlib.util,json,threading,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest import mock
spec=importlib.util.spec_from_file_location('linux_cdp',Path(__file__).resolve().parents[1]/'caps/cdp.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<title>Pylon CDP fixture</title><p id="test">ready</p>')
 def log_message(self,*a):pass
class Tests(unittest.TestCase):
 def tearDown(self):c.shutdown()
 def test_invalid_inputs(self):
  for url in ('file:///etc/passwd','javascript:alert(1)','https://a:b@example.com',''):
   self.assertFalse(c.METHODS['navigate']({'url':url},{})['ok'])
  self.assertIsNone(c._PROC)
  self.assertTrue(c.METHODS['stop']({}, {})['dry_run'])
 def test_no_browser_available(self):
  with mock.patch.object(c,'_browser',return_value=None):
   self.assertFalse(c.status({}, {})['available'])
   self.assertFalse(c.METHODS['navigate']({'url':'https://example.com'}, {})['ok'])
 def test_real_chromium(self):
  if not c._browser():self.skipTest('No Chromium executable')
  self.assertFalse(c.status({}, {})['running'])
  srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);th=threading.Thread(target=srv.serve_forever,daemon=True);th.start()
  try:
   r=c.navigate({'url':'http://127.0.0.1:'+str(srv.server_port)+'/?a=1&b=2'},{});self.assertTrue(r['ok']);tab=r['tab_id']
   import time
   for _ in range(30):
    r=c.evaluate({'tab_id':tab,'expr':'document.title'}, {})
    if r.get('value')=='Pylon CDP fixture':break
    time.sleep(.2)
   self.assertEqual(r['value'],'Pylon CDP fixture')
   self.assertEqual(c.evaluate({'tab_id':tab,'expr':'location.search'}, {})['value'],'?a=1&b=2')
   self.assertTrue(c.status({}, {})['running'])
   self.assertEqual(c._PROFILE.stat().st_mode&0o777,0o700)
   self.assertFalse(c.METHODS['eval']({'expr':'1+1'}, {})['ok'])
   self.assertTrue(c.close({'tab_id':tab}, {})['ok'])
   profile=c._PROFILE;c.shutdown();self.assertFalse(profile.exists());self.assertFalse(c.status({}, {})['running'])
  finally:srv.shutdown();srv.server_close()
