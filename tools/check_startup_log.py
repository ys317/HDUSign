"""Check frozen worker logs arrive while idle, before ASR or process exit."""
import json,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign.account_worker import ProcessServices
from hdusign.accounts import write_json
exe=Path(sys.argv[1]).resolve()
report_path=Path(sys.argv[2]).resolve()
with tempfile.TemporaryDirectory(prefix='hdusign-log-check-') as temporary:
    root=Path(temporary)
    lines=[]
    service=ProcessServices(root,log=lines.append)
    service.command=lambda directory:[str(exe),'--account-worker',str(directory)]
    service.credentials={'skl_username':'fixture-only','skl_password':'fixture-only'}
    write_json(root/'live_session.json',{'username':'fixture-only'})
    stop=threading.Event()
    values=[]
    errors=[]
    def run():
        try:values.append(service.run('isolation-check',{},stop,hold=True))
        except Exception as error:errors.append(type(error).__name__)
    thread=threading.Thread(target=run)
    started=time.monotonic()
    thread.start()
    try:
        deadline=started+15
        while time.monotonic()<deadline:
            if any('自检已就绪' in line for line in lines):break
            if errors:raise RuntimeError(str(errors))
            time.sleep(.02)
        else:raise RuntimeError('Startup log was buffered until process exit')
        report={'ok':thread.is_alive(),'log_delivered_before_exit':thread.is_alive(),
                'elapsed_seconds':round(time.monotonic()-started,2),'asr_started':False,
                'school_requests':0,'webhook_requests':0}
        assert report['ok']
    finally:
        stop.set();thread.join(15)
    assert not thread.is_alive() and service.process is None
    write_json(report_path,report)
    print(json.dumps(report,ensure_ascii=True))
