import pytest
from state_helpers import load


@pytest.mark.parametrize('platform,expected',[('darwin',['top','-l','1','-o','mem','-n','5','-stats','pid,command,mem']),('linux',['ps','-eo','pid,comm,rss','--sort=-rss'])])
def test_top_memory_command(platform,expected):
    assert load('top_mem').command(platform)==expected


def test_linux_prints_header_and_five_processes(monkeypatch,capsys):
    from types import SimpleNamespace
    m=load('top_mem');monkeypatch.setattr(m.sys,'platform','linux')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='\n'.join(['PID COMM RSS']+[f'{i} python {100-i}' for i in range(8)])+'\n',stderr=''))
    assert m.main()==0
    assert capsys.readouterr().out=='PID COMM RSS\n0 python 100\n1 python 99\n2 python 98\n3 python 97\n4 python 96\n'
