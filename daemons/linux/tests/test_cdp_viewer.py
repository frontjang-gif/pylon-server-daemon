import importlib.util,os,shutil,tempfile,threading,time,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest import mock
spec=importlib.util.spec_from_file_location('linux_viewer_test',Path(__file__).resolve().parents[1]/'caps/cdp.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<title>Viewer test</title><input id="field" style="width:250px;height:40px" autofocus>')
 def log_message(self,*a):pass
class Tests(unittest.TestCase):
 def tearDown(self):c.shutdown()
 def test_names_and_storage(self):
  for name in ('../x','a/b','Upper','a'*33):
   with self.assertRaises(ValueError):c._profile_name(name)
  with mock.patch.object(c,'_free',return_value=100):
   self.assertFalse(c._persistent_space(Path('/tmp'),True))
   with self.assertRaisesRegex(ValueError,'reserve'):c._persistent('test')
 def test_real_viewer_and_profile(self):
  if not c._browser():self.skipTest('Chromium unavailable')
  root=Path(tempfile.mkdtemp(prefix='viewer-test-',dir='/dev/shm'))
  srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
  url='http://127.0.0.1:'+str(srv.server_port)
  ctx={'user':'fixture-owner'}
  try:
   # Use disposable tmpfs profile, simulate ample capacity only in this test.
   with mock.patch.object(c,'_root',return_value=root),mock.patch.object(c,'_persistent_space',return_value=True):
    r=c.viewer_open({'url':url,'profile':'test-profile','confirm':True},ctx);sid=r['session'];q={'session':sid}
    time.sleep(.3)
    self.assertFalse(c.METHODS['viewer_shot'](q,{'user':'wrong'})['ok'])
    self.assertFalse(c.METHODS['eval']({'profile':'test-profile','tab_id':c._VIEWER['tab'],'expr':'document.title'},ctx)['ok'])
    frame=c.viewer_shot(q,ctx);self.assertTrue(frame['img']);self.assertEqual(frame['w'],1200)
    c.viewer_input({**q,'kind':'click','payload':{'xf':.08,'yf':.03}},ctx)
    c.viewer_input({**q,'kind':'type','payload':{'text':'fixture-text'}},ctx)
    t=c._target(c._VIEWER['tab'])
    val=c._command(t,'Runtime.evaluate',{'expression':'document.querySelector("#field").value','returnByValue':True})['result']['value']
    self.assertEqual(val,'fixture-text')
    c._command(t,'Runtime.evaluate',{'expression':'localStorage.setItem("fixture","persisted")'})
    self.assertFalse(c.stop({'confirm':True},ctx)['ok'])
    c.viewer_stop(q,ctx)
    self.assertTrue((root/'test-profile').exists())
    self.assertEqual((root/'test-profile').stat().st_mode&0o777,0o700)
    r=c.navigate({'url':url,'profile':'test-profile'},ctx);time.sleep(.4)
    self.assertEqual(c.evaluate({'profile':'test-profile','tab_id':r['tab_id'],'expr':'localStorage.getItem("fixture")'},ctx)['value'],'persisted')
    c.shutdown()
    self.assertFalse(c.METHODS['viewer_shot'](q,ctx)['ok'])
  finally:c.shutdown();srv.shutdown();srv.server_close();shutil.rmtree(root,ignore_errors=True)
