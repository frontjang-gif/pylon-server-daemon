import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from daemons import work_sdk as S
class SDK(unittest.TestCase):
    def test_mapping_and_publish(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);mount=root/'mount';mount.mkdir();conf=root/'mounts.json'
            conf.write_text(json.dumps([{'prefix':'vfs://alias://dataset','root':str(mount),'mode':'read_write'}]))
            with patch.object(S,'CONFIG',conf):
                src=root/'input';src.write_bytes(b'hello')
                r=S.publish(src,'vfs://alias://dataset/result.txt');self.assertEqual(r['sha256'],hashlib.sha256(b'hello').hexdigest())
                self.assertEqual(r['durability'],'endpoint_local_verified')
                with S.open_input('vfs://alias://dataset/result.txt') as f:self.assertEqual(f.read(),b'hello')
                with self.assertRaises(FileExistsError):S.publish(src,'vfs://alias://dataset/result.txt')
                for url in ('vfs://alias://dataset/../input','vfs://alias://dataset-other/result.txt','vfs://alias://dataset/a%2f..','vfs://alias://dataset/CON.txt'):
                    with self.assertRaises(ValueError):S.resolve(url)
                (mount/'escape').symlink_to(root,target_is_directory=True)
                with self.assertRaises(ValueError):S.resolve('vfs://alias://dataset/escape/input')
                conf.write_text(json.dumps([{'prefix':'vfs://alias://dataset','root':str(mount),'mode':'read'}]))
                with self.assertRaises(PermissionError):S.publish(src,'vfs://alias://dataset/new')
    def test_progress(self):
        with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,{'PYLON_WORK_PROGRESS_FILE':str(Path(t)/'progress.json')}):
            p=Path(t)/'progress.json';S.report(3,10,'processing');self.assertEqual(S.read_progress(p),{'done':3,'total':10,'detail':'processing'})
            for a in ((11,10,'invalid'),(float('nan'),10,'invalid'),(1,10,'password=example')):
                with self.assertRaises(ValueError):S.report(*a)
            p.write_bytes(b'x'*5000);self.assertIsNone(S.read_progress(p))
