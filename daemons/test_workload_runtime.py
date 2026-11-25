import tempfile,unittest,time,sys
from pathlib import Path
from unittest.mock import patch
from daemons import workload_runtime as W
class Workloads(unittest.TestCase):
    def wait(self,jid):
        end=time.monotonic()+15
        while time.monotonic()<end:
            j=W.status({'job_id':jid},{})['job']
            if j['status'] not in ('queued','running','stop_requested'):return j
            time.sleep(.05)
        self.fail('Endpoint job did not terminate')
    def test_execution_and_idempotency(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as t,patch.object(W,'ROOT',Path(t)):
            a={'job_id':'a'*32,'cmd':"""python3 -c "from pathlib import Path; Path('result.txt').write_text(str(sum(range(100000)))); print('x'*100000)" """,'timeout':10}
            W.submit(a,{})
            j=self.wait(a['job_id'])
            self.assertEqual(j['status'],'completed');self.assertTrue(j['truncated']);self.assertLessEqual(len(j['stdout']),W.TAIL)
            self.assertEqual((Path(j['workdir'])/'result.txt').read_text(),'4999950000')
            self.assertEqual(W.submit(a,{})['job']['status'],'completed')
            with self.assertRaises(ValueError):W.submit({**a,'cmd':'echo different'},{})
    def test_cancel_and_interrupted(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as t,patch.object(W,'ROOT',Path(t)):
            a={'job_id':'b'*32,'cmd':'python3 -c "import time; time.sleep(30)"','timeout':60}
            W.submit(a,{})
            W.cancel({'job_id':a['job_id']},{})
            self.assertEqual(self.wait(a['job_id'])['status'],'cancelled')
            p=W.path('c'*32);p.parent.mkdir();W.save(p,{'id':'c'*32,'fingerprint':'fixture','status':'running'})
            self.assertEqual(W.status({'job_id':'c'*32},{})['job']['status'],'interrupted')
