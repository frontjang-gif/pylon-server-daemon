import importlib.util,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('viewer_recipe_test',Path(__file__).resolve().parents[1]/'caps/cdp.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Tests(unittest.TestCase):
 def test_recipe_checks_and_redaction(self):
  with patch.object(c,'_viewer',return_value={'id':'fixture-tab'}) as viewer,patch.object(c,'_command',return_value={'result':{'value':{'state':'prepared','fields':['name','secret'],'secret':'fixture'}}}) as cmd:
   self.assertFalse(c.viewer_recipe({'expr':'fixture'}, {})['ok']);cmd.assert_not_called()
   r=c.viewer_recipe({'session':'fixture','expr':'fixture','confirm':True},{'user':'owner'})
   self.assertEqual(r,{'ok':True,'state':'prepared','fields':['name']})
   viewer.assert_called_once();self.assertEqual(cmd.call_args.args[0],{'id':'fixture-tab'})
 def test_invalid_session(self):
  self.assertFalse(c.METHODS['viewer_recipe']({'session':'unknown','expr':'fixture','confirm':True},{'user':'owner'})['ok'])
