import sys, os, asyncio, threading, socket, subprocess, json, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
import test_node_leases as t
import uvicorn, httpx
from datetime import timedelta
E=Path('/Users/michalsipa/.no-mistakes/evidence/01M5S6XDDDDK9DJTGD2SHF4JZ4')
log=[]
app=t._app(dispatch_wait_timeout=4, settings=t._settings(ttl=timedelta(seconds=2),expiring_soon=timedelta(seconds=1)))
sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
th=threading.Thread(target=lambda:server.run(sockets=[sock])); th.start()
while not server.started: time.sleep(.01)
base=f'http://127.0.0.1:{port}'
c=httpx.Client(base_url=base,headers=t.AUTH_HEADERS)
def req(method,path,**kw):
 r=c.request(method,path,**kw); b=r.json(); safe={k:v for k,v in b.items() if k!='token'}; log.append({'method':method,'path':path,'status':r.status_code,'body':safe,'backend':r.headers.get('X-Backend-Id')}); return r
try:
 for h in ['host-a','host-b']:
  assert req('POST','/v1/nodes/register',json={'host_id':h,'backend_type':'fake','base_url':'http://fake','context_window':8192,'max_concurrent_requests':1,'allowed_models':['gemma']}).status_code==200
 lease=req('POST','/v1/node-leases',json={'holder':'live-check','host_id':'host-a'}).json(); token=lease['token']
 assert req('POST','/v1/chat/completions',json=t._chat()).headers['X-Backend-Id']=='host-b'
 assert req('POST','/v1/chat/completions',json=t._chat(),headers={t.LEASE_HEADER_NAME:token}).headers['X-Backend-Id']=='host-a'
 assert req('POST','/v1/node-leases',json={'holder':'other','host_id':'host-a'}).status_code==409
 assert req('POST','/v1/node-leases',json={'holder':'other','host_id':'host-b'}).status_code==409
 assert req('POST','/v1/node-leases/heartbeat',headers={t.LEASE_HEADER_NAME:token}).status_code==200
 async def screenshot():
  from llm_home_lab.tui.app import DashboardApp
  class Client:
   async def list_nodes(self): return c.get('/v1/nodes').json()
   async def fetch_metrics_text(self): return c.get('/metrics').text
  ui=DashboardApp(client=Client(),interval_s=100)
  async with ui.run_test(size=(180,40)) as pilot:
   await ui.poll(); await pilot.pause(); (E/'lease-dashboard.svg').write_text(ui.export_screenshot())
 asyncio.run(screenshot())
 time.sleep(2.1)
 assert req('POST','/v1/chat/completions',json=t._chat(),headers={t.LEASE_HEADER_NAME:token}).status_code in (403,409)
 assert all(n['lease'] is None for n in req('GET','/v1/nodes').json()['nodes'])

 # Saturate disposable backend capacity, then exercise real HTTP waiter ownership.
 app.state.registry.acquire_slot('host-a'); app.state.registry.acquire_slot('host-b')
 results={}
 def chat(name,headers=None): results[name]=c.post('/v1/chat/completions',json=t._chat(),headers=headers)
 ordinary=threading.Thread(target=chat,args=('ordinary',)); ordinary.start()
 while app.state.scheduling_queue.depth()!=1: time.sleep(.01)
 held=req('POST','/v1/node-leases',json={'holder':'queue-check','host_id':'host-a'}).json()
 pinned=threading.Thread(target=chat,args=('pinned',{t.LEASE_HEADER_NAME:held['token']})); pinned.start()
 while app.state.scheduling_queue.depth()!=2: time.sleep(.01)
 app.state.registry.release_slot('host-a'); pinned.join(2)
 assert results['pinned'].headers['X-Backend-Id']=='host-a' and ordinary.is_alive()
 app.state.registry.release_slot('host-b'); ordinary.join(2)
 assert results['ordinary'].headers['X-Backend-Id']=='host-b'
 log.append({'queue':'pinned admitted on A; earlier ordinary entry retained and admitted on B','ordinary_status':results['ordinary'].status_code,'pinned_status':results['pinned'].status_code})
 req('POST','/v1/node-leases/force-release',json={'host_id':'host-a'})
 for mode in ['expiry','force-release']:
  held=req('POST','/v1/node-leases',json={'holder':'old','host_id':'host-a'}).json()
  app.state.registry.acquire_slot('host-a')
  worker=threading.Thread(target=chat,args=('stale',{t.LEASE_HEADER_NAME:held['token']})); worker.start()
  while app.state.scheduling_queue.depth()!=1: time.sleep(.01)
  if mode=='expiry': time.sleep(2.1)
  else: req('POST','/v1/node-leases/force-release',json={'host_id':'host-a'})
  assert req('POST','/v1/node-leases',json={'holder':'replacement','host_id':'host-a'}).status_code==201
  app.state.registry.release_slot('host-a'); worker.join(3)
  assert results['stale'].status_code in (403,409)
  log.append({'stale_waiter':mode,'status':results['stale'].status_code,'body':results['stale'].json()})
  req('POST','/v1/node-leases/force-release',json={'host_id':'host-a'})
 prefix=[sys.executable,'-m','llm_home_lab.leases.runner','--base-url',base,'--api-key','test-key','--node','host-a','--holder','runner-check']
 for code in ["import os; print('child node='+os.environ['LLM_HOME_LAB_NODE_ID']); raise SystemExit(7)","import os,signal; os.kill(os.getpid(),signal.SIGINT)"]:
  p=subprocess.run(prefix+['--',sys.executable,'-c',code],capture_output=True,text=True)
  log.append({'runner_exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr}); assert p.returncode in (7,130)
  assert all(n['lease'] is None for n in req('GET','/v1/nodes').json()['nodes'])
finally:
 server.should_exit=True; th.join(5); c.close(); (E/'live-transcript.json').write_text(json.dumps(log,indent=2))
print('HTTP routing, refusal, heartbeat, TTL, runner exit/cleanup and dashboard capture passed')
