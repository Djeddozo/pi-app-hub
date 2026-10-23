import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from hub.discovery import DockerProvider, ManualProvider, Manifest, SystemdProvider, UserSystemdProvider
from hub.main import create_app
from hub.manager import Manager
from hub.registry import Registry
from hub.security import docker_id, protected, redact, safe_path, safe_url, service_name
from hub.system import System

def snapshot(source='docker', key='compose:finance'):
    return {'key':key,'source':source,'runtime':'Docker Compose','name':'Finance Vault','icon':'wallet',
            'status':'Running','protected':False,'members':[{'id':'a'*64,'name':'finance-web','state':'running','role':'web','depends_on':''}],
            'ports':[],'project_path':'/opt/finance','data_directories':[{'path':'/opt/finance/data','type':'bind'}],
            'uptime':10,'web_url':None,'tailscale_url':None}

@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path/'registry.db')

def test_registry_preserves_edits_and_marks_missing(registry):
    app = snapshot()
    assert registry.reconcile('docker',[app]) == {'new':1,'updated':0,'missing':0}
    app_id = registry.list()[0]['id']
    registry.edit(app_id, {'name':'My finance','protected':True})
    app['name']='New discovery name'
    app['uptime']=50
    registry.reconcile('docker',[app])
    assert registry.get(app_id)['name']=='My finance'
    assert registry.get(app_id)['protected']
    assert registry.reconcile('docker',[])['missing']==1
    assert registry.get(app_id)['status']=='Missing'
    assert registry.reconcile('docker',[app])['updated']==1
    assert registry.get(app_id)['status']=='Running'

def test_uptime_is_not_an_update(registry):
    app=snapshot();registry.reconcile('docker',[app]);app['uptime']=100
    assert registry.reconcile('docker',[app])['updated']==0

def test_protection_cannot_be_overridden(registry):
    app=snapshot();app['protected']=True;registry.reconcile('docker',[app])
    app_id=registry.list()[0]['id'];registry.edit(app_id,{'protected':False})
    assert registry.get(app_id)['protected']

@pytest.mark.parametrize('value',['../../evil.service','--help','foo.service;id','foo\n.service'])
def test_invalid_service(value):
    with pytest.raises(ValueError):service_name(value)

@pytest.mark.parametrize('value',['/','/home','/etc','relative/path','/tmp/../etc','/tmp/\x00file'])
def test_invalid_paths(value):
    with pytest.raises(ValueError):safe_path(value)

@pytest.mark.parametrize('value',['javascript:alert(1)','https://user:secret@example.org','file:///etc/passwd','http://localhost:999999'])
def test_invalid_urls(value):
    with pytest.raises(ValueError):safe_url(value)

@pytest.mark.parametrize('value',['tailscaled.service','docker.service','ssh.service','NetworkManager.service','wifi-reconnect.service','pi-app-hub.service','systemd-networkd.service'])
def test_critical(value):assert protected(value)

def test_redaction():
    masked=redact('password=hello API_KEY=secret Authorization: Bearer abc123 https://user:pass@host ENV_NAME=sensitive')
    for secret in ['hello','secret','abc123','user:pass','sensitive']:assert secret not in masked
    assert 'normal log message'==redact('normal log message')

def test_compose_grouping():
    def container(char,name,project):
        labels={'com.docker.compose.project':project} if project else {}
        return SimpleNamespace(id=char*64,name=name,status='running',labels=labels,
            attrs={'State':{'Running':True,'StartedAt':'2026-01-01T00:00:00Z'},'Config':{'Image':'app'},
                   'NetworkSettings':{'Ports':{}},'Mounts':[]})
    provider=DockerProvider(Mock(url_for_ports=Mock(return_value=None)))
    provider.client=Mock();provider.client.containers.list.return_value=[container('a','finance-web','finance-vault'),container('b','finance-db','finance-vault'),container('c','redis','finance-vault'),container('d','other',None)]
    apps=provider.discover();assert len(apps)==2
    finance=next(a for a in apps if a['name']=='Finance Vault')
    assert finance['runtime']=='Docker Compose' and len(finance['members'])==3

def test_failed_provider_does_not_mark_missing(registry,tmp_path):
    registry.reconcile('docker',[snapshot()])
    manager=Manager(registry,System(),tmp_path)
    provider=Mock(source='docker');provider.discover.side_effect=RuntimeError('offline');manager.providers=[provider]
    assert manager.scan()['missing']==0
    assert registry.list()[0]['status']=='Running'
    assert not manager.health['docker']['ok']

def test_manifest_rejects_commands_and_traversal(tmp_path):
    with pytest.raises(ValueError):Manifest(id='../bad',name='Bad')
    with pytest.raises(ValueError):Manifest(id='okay',name='Bad',command='rm -rf /')
    with pytest.raises(ValueError):Manifest(id='okay',name='Bad',data_directories=['/'])
    (tmp_path/'app.json').write_text('{"id":"demo","name":"Demo","web_url":"http://localhost:1234"}')
    assert ManualProvider(tmp_path).discover()[0]['runtime']=='manual'

def fake_manager(registry,tmp_path):
    app=snapshot();registry.reconcile('docker',[app])
    manager=Manager(registry,System(),tmp_path)
    provider=Mock(source='docker');provider.discover.side_effect=lambda:[copy.deepcopy(app)]
    manager.providers=[provider]
    manager.docker.client=Mock()
    return manager,app,registry.list()[0]['id']

def test_uninstall_requires_exact_name_and_consumes_token(registry,tmp_path):
    manager,app,app_id=fake_manager(registry,tmp_path)
    preview=manager.preview(app_id)
    with pytest.raises(ValueError):manager.uninstall(app_id,preview['token'],'wrong')
    with pytest.raises(ValueError):manager.uninstall(app_id,preview['token'],app['name'])
    manager.docker.client.containers.get.assert_not_called()

def test_uninstall_detects_members_changed(registry,tmp_path):
    manager,app,app_id=fake_manager(registry,tmp_path);preview=manager.preview(app_id)
    app['members'].append({'id':'b'*64,'name':'db','state':'running'})
    with pytest.raises(ValueError,match='changed'):manager.uninstall(app_id,preview['token'],app['name'])
    manager.docker.client.containers.get.assert_not_called()

def test_uninstall_preserves_volumes(registry,tmp_path):
    manager,app,app_id=fake_manager(registry,tmp_path);preview=manager.preview(app_id)
    manager.uninstall(app_id,preview['token'],app['name'])
    manager.docker.client.containers.get.return_value.remove.assert_called_once_with(v=False,force=False)
    assert registry.get(app_id) is None
    registry.reconcile('docker',[app]);assert registry.list()==[]

def test_protected_action_is_rejected(registry,tmp_path):
    manager,app,app_id=fake_manager(registry,tmp_path);app['protected']=True
    with pytest.raises(ValueError,match='protected'):manager.action(app_id,'stop')
    manager.docker.client.containers.get.assert_not_called()

def test_compose_dependency_order():
    app={'members':[{'id':'web','role':'web','depends_on':'db:service_healthy:false'},{'id':'db','role':'db','depends_on':''}]}
    assert [m['id'] for m in Manager.ordered_members(app)]==['db','web']

def test_no_arbitrary_action(registry,tmp_path):
    manager,_,app_id=fake_manager(registry,tmp_path)
    with pytest.raises(ValueError):manager.action(app_id,'exec')

def test_api_csrf_hosts_validation_and_audit(tmp_path):
    app=create_app(tmp_path,background=False);registry=app.state.registry
    app.state.manager.scan=Mock(return_value={'new':1,'updated':0,'missing':0})
    registry.reconcile('docker',[snapshot()]);app_id=registry.list()[0]['id']
    with TestClient(app,base_url='http://127.0.0.1:8840') as client:
        assert client.get('/api/apps',headers={'host':'evil.example'}).status_code==400
        assert client.patch(f'/api/apps/{app_id}',json={'name':'Edited'}).status_code==403
        headers={'x-pi-hub':'1','origin':'https://evil.example'}
        assert client.patch(f'/api/apps/{app_id}',json={'name':'Edited'},headers=headers).status_code==403
        headers={'x-pi-hub':'1','origin':'http://127.0.0.1:8840'}
        assert client.patch(f'/api/apps/{app_id}',json={'members':[]},headers=headers).status_code==422
        assert client.patch(f'/api/apps/{app_id}',json={'project_path':'/'},headers=headers).status_code==422
        assert client.patch(f'/api/apps/{app_id}',json={'name':'Edited'},headers=headers).status_code==200
        assert registry.get(app_id)['name']=='Edited'
        assert registry.activity()[0]['outcome']=='success'
        assert client.get('/api/apps').headers['cache-control']=='no-store'
        assert client.post('/api/apps/register',json={'id':'demo','name':'Demo'},headers=headers).status_code==200

def test_existing_tailscale_route_is_preferred():
    system=System();system.tailscale=Mock(return_value={'hostname':'pi.ts.net','ips':['100.1.1.1'],'routes':{'Web':{'pi.ts.net:8444':{'Handlers':{'/':{'Proxy':'http://localhost:8765'}}}}}})
    assert system.url_for_ports([{'host_port':8765,'host_ip':'127.0.0.1'}])=='https://pi.ts.net:8444/'
    assert system.url_for_ports([{'host_port':1234,'host_ip':'127.0.0.1'}]) is None

def test_root_helper_rejects_critical_and_arbitrary_arguments():
    spec=importlib.util.spec_from_file_location('helper',Path(__file__).parents[1]/'deploy/service-helper.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    for args in [('exec','foo.service'),('stop','../../evil.service'),('uninstall','ssh.service'),('stop','wifi-reconnect.service')]:
        with pytest.raises(ValueError):helper.main(args)

def test_normal_systemd_service_is_manageable(registry,tmp_path,monkeypatch):
    app=snapshot('systemd','systemd:demo.service')
    app.update(runtime='systemd',members=[{'id':'demo.service','name':'demo.service','state':'active'}])
    registry.reconcile('systemd',[app]);app_id=registry.list()[0]['id']
    manager=Manager(registry,System(),tmp_path)
    provider=Mock(source='systemd');provider.discover.return_value=[app];manager.providers=[provider]
    call=Mock();monkeypatch.setattr('hub.manager.command',call)
    manager.action(app_id,'restart')
    assert call.call_args.args[0][-2:]==['restart','demo.service']

def test_unset_systemd_counters_do_not_break_discovery(monkeypatch):
    system=Mock();system.listeners.return_value=[];system.url_for_ports.return_value=None
    provider=SystemdProvider(system);provider.units=Mock(return_value=[Path('/etc/systemd/system/demo.service')])
    monkeypatch.setattr('hub.discovery.command',lambda args:'Id=demo.service\nDescription=Demo\nActiveState=inactive\nMainPID=0\nCPUUsageNSec=[not set]\nMemoryCurrent=[not set]\n')
    app=provider.discover()[0]
    assert app['status']=='Stopped' and app['ram']==0

def test_user_systemd_provider_is_explicit(monkeypatch,tmp_path):
    provider=UserSystemdProvider(System()); provider.unit_dir=tmp_path
    unit=tmp_path/'ticket-watcher.service';unit.write_text('[Service]\nExecStart=/bin/true\n')
    monkeypatch.setattr('hub.discovery.subprocess.run',lambda *args,**kwargs: SimpleNamespace(returncode=0,stdout='Id=ticket-watcher.service\nDescription=HelloAsso Ticket Watcher\nActiveState=active\nMainPID=0\nCPUUsageNSec=0\nMemoryCurrent=0\n'))
    app=provider.discover()[0]
    assert app['source']=='systemd-user' and app['scope']=='user' and app['runtime']=='systemd'

def test_user_systemd_uninstall_is_preserved(registry,tmp_path):
    app=snapshot('systemd-user','user:ticket-watcher.service');app.update(runtime='systemd',scope='user',members=[{'id':'ticket-watcher.service','name':'ticket-watcher.service','state':'active'}])
    registry.reconcile('systemd-user',[app]);app_id=registry.list()[0]['id']
    manager=Manager(registry,System(),tmp_path);provider=Mock(source='systemd-user');provider.discover.return_value=[app];manager.providers=[provider]
    with pytest.raises(ValueError,match='not removed'):manager.preview(app_id)

def test_manifest_watcher_discovers_without_scan_button(registry,tmp_path):
    import time
    directory=tmp_path/'manifests'
    manager=Manager(registry,System(),directory)
    manager.providers=[ManualProvider(directory)]
    manager.events_loop=lambda:None;manager.metrics_loop=lambda:None
    manager.start()
    try:
        deadline=time.monotonic()+5
        while manager.last_scan is None and time.monotonic()<deadline:time.sleep(.05)
        (directory/'watched.json').write_text('{"id":"watched","name":"Watched app"}')
        while not registry.list() and time.monotonic()<deadline:time.sleep(.05)
        assert registry.list()[0]['name']=='Watched app'
    finally:manager.close()

def test_expired_preview_is_rejected(registry,tmp_path):
    manager,app,app_id=fake_manager(registry,tmp_path);preview=manager.preview(app_id)
    manager.previews[preview['token']]['expires']=0
    with pytest.raises(ValueError,match='expired'):manager.uninstall(app_id,preview['token'],app['name'])
    manager.docker.client.containers.get.assert_not_called()
