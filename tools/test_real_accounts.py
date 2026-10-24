"""Opt-in diagnostic for the two user-authorized real accounts. No sign-in/push."""
import concurrent.futures
import copy
from dataclasses import asdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign.accounts import AccountStore,read_json,write_json
from hdusign.account_worker import ProcessServices
import yaml
ROOT=Path(__file__).resolve().parents[2]
EXE=ROOT/'HDUSign-continuous-v2.1.0.exe'
OUT=ROOT/'source/build/real-account-isolation'
OUT.mkdir(exist_ok=True)
USERS=tuple(sys.argv[1:])
if not USERS:raise SystemExit('Pass the explicitly authorized account IDs as arguments')
def emit(user,event,**data):
    print(json.dumps(dict(account=user,event=event,**data),ensure_ascii=False),flush=True)
def prepare(user):
    store=AccountStore(ROOT)
    root=ROOT/'account_data'/hashlib.sha256(user.encode()).hexdigest()
    root.mkdir(parents=True,exist_ok=True)
    service=ProcessServices(root,log=lambda line: emit(user,'worker',message=line) if ('登录' in line or '[错误]' in line) else None)
    service.command=lambda directory:[str(EXE),'--account-worker',str(directory)]
    service.credentials={'skl_username':user,'skl_password':store.password(user)}
    cfg=yaml.safe_load((root/'config.yaml').read_text('utf-8'))
    cfg.setdefault('auto_sign',{})['enabled']=False
    cfg.setdefault('alert',{}).setdefault('webhook',{})['url']=''
    cfg['live_audio']={'enabled':True,'continuous':True}
    stop=threading.Event()
    saved=read_json(root/'live_session.json')
    if not saved:
        emit(user,'login-start')
        result=service.login('https://course.hdu.edu.cn/#/home',stop,service.credentials)
        if result!=0:raise RuntimeError('Login did not finish')
    try:
        courses=service.courses(cfg,stop)
    except Exception:
        emit(user,'relogin-start')
        result=service.login('https://course.hdu.edu.cn/#/home',stop,service.credentials)
        if result!=0:raise RuntimeError('Login did not finish')
        courses=service.courses(cfg,stop)
    saved=read_json(root/'live_session.json')
    now=time.time()
    active=[c for c in courses if c.start-120<=now<c.end+120]
    summary=[dict(course_id=c.course_id,title=c.title,start=c.start,end=c.end,lesson_ids=c.lesson_ids) for c in courses]
    write_json(OUT/(user+'-courses.json'),{'account':user,'identity_matches':saved.get('user_code',saved.get('username'))==user,'courses':[asdict(c) for c in courses],'captured_at':now})
    emit(user,'courses-ready',identity_matches=saved.get('user_code',saved.get('username'))==user,active=[dict(id=c.course_id,title=c.title) for c in active],courses=summary)
    return user
if __name__=='__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(prepare,user):user for user in USERS}
        for future in concurrent.futures.as_completed(futures):
            try:future.result()
            except Exception as exc:emit(futures[future],'failed',error=type(exc).__name__)
