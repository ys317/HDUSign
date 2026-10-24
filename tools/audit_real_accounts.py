"""Run authorized-account probes concurrently with explicitly scoped envs."""
import concurrent.futures,json,os,subprocess,sys,threading,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign.account_worker import ProcessServices
from hdusign.accounts import AccountStore,read_json,write_json
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'source/build/real-account-isolation'
USERS=tuple(sys.argv[1:])
if len(USERS)!=2:raise SystemExit('Pass exactly two authorized account IDs')
store=AccountStore(ROOT)
session_before={user:hashlib.sha256((ROOT/'account_data'/hashlib.sha256(user.encode()).hexdigest()/'live_session.json').read_bytes()).hexdigest() for user in USERS}
def service_for(user, credential_user=None):
    folder=ROOT/'account_data'/hashlib.sha256(user.encode()).hexdigest()
    service=ProcessServices(folder)
    credential_user=credential_user or user
    service.credentials={'skl_username':credential_user,'skl_password':store.password(credential_user)}
    service.command=lambda directory:[str(ROOT/'HDUSign-continuous-v2.1.0.exe'),'--account-worker',str(directory)]
    return service
def run(user):
    service=service_for(user)
    report=OUT/(user+'-stream-audit.json')
    process=subprocess.run([sys.executable,str(ROOT/'source/tools/probe_real_stream.py'),user,str(report)],env=service.environment(service.credentials),capture_output=True,timeout=50)
    if process.returncode!=0:
        print(json.dumps({'account':user,'probe_failed':True,'exit_code':process.returncode}),flush=True)
    if report.exists():print(json.dumps(read_json(report),ensure_ascii=True),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(run,USERS))
negative=service_for(USERS[1],USERS[0])
rejected=False
try:negative.courses({'live_audio':{'enabled':True}},threading.Event())
except Exception as error:rejected='不一致' in str(error)
session_unchanged={user:hashlib.sha256((ROOT/'account_data'/hashlib.sha256(user.encode()).hexdigest()/'live_session.json').read_bytes()).hexdigest()==session_before[user] for user in USERS}
summary={'wrong_account_root_rejected':rejected,'sessions_unchanged':session_unchanged,'reports':[read_json(OUT/(user+'-stream-audit.json')) for user in USERS]}
write_json(OUT/'summary.json',summary)
print(json.dumps({'wrong_account_root_rejected':rejected},ensure_ascii=True),flush=True)
