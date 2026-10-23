"""Offline regression for per-account quota page isolation."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # engine.py imports proxy_support
sys.modules['cdp'] = SimpleNamespace(CDP=object, http_json=lambda *args, **kwargs: {})
engine_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'engine.py'
spec = importlib.util.spec_from_file_location('candidate_engine', engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

instance = engine.MuseEngine.__new__(engine.MuseEngine)
seen = []
responses = iter([
    '{"x": 1, "y": 2}', '{"x": 1, "y": 2}',
    'Usage\nFree plan\nWeekly limit resets on Oct 17\n2% used\nAdditional tokens\nNever expires\n0% used (8B tokens left)',
    '{"x": 1, "y": 2}', '{"x": 1, "y": 2}',
    'Usage\nFree plan\nWeekly limit resets on Oct 17\n24% used\nAdditional tokens\nNever expires\n0% used (1B tokens left)',
])

class Page:
    def js(self, script):
        return next(responses)
    def send(self, *args, **kwargs):
        pass

def ensure(cookies, expires, account_id=None):
    seen.append(account_id)
    instance.page = Page()
    return instance.page

instance.ensure_page = ensure
instance._click_point = lambda x, y: None
with patch('time.sleep'):
    qa = instance.quota({'fixture': 'a'}, account_id='account-a')
    qb = instance.quota({'fixture': 'b'}, account_id='account-b')
assert seen == ['account-a', 'account-b']
assert qa['weekly_used_pct'] == 2 and qa['extra_left'] == '8B tokens left'
assert qb['weekly_used_pct'] == 24 and qb['extra_left'] == '1B tokens left'
print('PASS: account-bound quota reads remain isolated across accounts')
