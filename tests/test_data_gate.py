"""Public data-leak gate: synthetic violations must be caught inside every container format."""
import importlib.util
import io
import sqlite3
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('data_gate', ROOT / 'scripts/data_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# 人工构造的违规样例（均为虚构）：内部域名、非示例邮箱、手机号、校验位正确的身份证与信用代码。
BAD = ('host: erp.' + 'kingdee.com' + '\nmail: someone@qq.com\nphone: 13912345678\n'
       'id: 110101199003071233\nuscc: 91350100M000100Y43\n')


def test_plain_text_rules():
    assert set(gate.scan_text(BAD)) == {'internal_host', 'email', 'mobile', 'id_card', 'uscc'}


def test_allowed_values_pass():
    text = ('https://vip.' + 'kingdee.com/knowledge/9609 supplier-contact@example.com '
            'uscc 91000000MK0000001C id 1000000000000000001 amount 13800000000.00')
    assert gate.scan_text(text) == {}


def test_containers_are_unpacked(tmp_path):
    xlsx = io.BytesIO()
    with zipfile.ZipFile(xlsx, 'w') as z:
        z.writestr('xl/sharedStrings.xml', '<sst><si><t>someone@qq.com</t></si></sst>')
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode='w:gz') as t:
        data = b'see https://erp-test.' + b'kingdee.com/x'
        info = tarfile.TarInfo('input/a.txt'); info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    db = tmp_path / 'm.db'
    con = sqlite3.connect(db); con.execute('create table t(v text)'); con.execute("insert into t values ('13912345678')")
    con.commit(); con.close()
    (tmp_path / 'a.xlsx').write_bytes(xlsx.getvalue())
    (tmp_path / 'w.tar.gz').write_bytes(tar.getvalue())
    (tmp_path / 'broken.xlsx').write_bytes(b'not a zip')
    errors = gate.scan_paths(sorted(tmp_path.iterdir()), tmp_path)
    assert 'data leak email: a.xlsx!xl/sharedStrings.xml (1)' in errors
    assert any(e.startswith('data leak internal_host: w.tar.gz!input/a.txt') for e in errors)
    assert 'data leak mobile: m.db!t (1)' in errors
    assert 'data scan failed: broken.xlsx (BadZipFile)' in errors
    assert not any('qq.com' in e or '13912345678' in e for e in errors)  # 只报规则与位置，不回显原值
