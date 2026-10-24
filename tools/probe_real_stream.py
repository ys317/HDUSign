"""Read-only live routing audit; writes sanitized evidence, never signs/pushes."""
from contextlib import closing
from dataclasses import asdict
import hashlib,json,os,sys,threading,time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign import live
from hdusign.lessons import LessonPlan,DeadlineStop
from hdusign.media import FFmpegAudioSource,MediaError
from hdusign.accounts import load_credentials,read_json,write_json
ROOT=Path(__file__).resolve().parents[2]
user=sys.argv[1]
folder=ROOT/'account_data'/hashlib.sha256(user.encode()).hexdigest()
if Path(os.environ['HDUSIGN_ACCOUNT_ROOT']).resolve()!=folder.resolve():raise RuntimeError('wrong root')
record=read_json(ROOT/'source/build/real-account-isolation'/(user+'-courses.json'))
rows=[live.LiveCourse(**row) for row in record['courses']]
now=time.time()
course=next((c for c in rows if c.start-120<=now<c.end+120),None)
if course is None:course=next((c for c in rows if c.start>now),rows[-1])
plan=LessonPlan.from_mapping(asdict(course),course.course_id)
window=plan.window(now) if plan else (course.course_id,course.end+120)
selected_id,deadline=window
creds=load_credentials(folder)
report={'account':user,'captured_at':now,'identity_matches':False,'selected_title':course.title,'selected_base_id':course.course_id,'expected_stream_id':selected_id,'course_start':course.start,'course_end':course.end,'in_monitor_window':course.start-120<=now<course.end+120,'attempts':[],'audio_seconds':0.0,'sign_requests':0,'webhook_requests':0}
cfg={'live_url':live.live_course_url(course),'auto_sign':{'enabled':False},'alert':{'webhook':{'url':''}}}
session,_=live._authenticated_session(live.parse_live_origin(cfg['live_url']),cfg)
origin=live.parse_live_origin(cfg['live_url'])
def fingerprint(session):
    return hashlib.sha256(json.dumps({'headers':dict(session.headers),'cookies':sorted((c.name,c.value,c.domain,c.path) for c in session.cookies)},sort_keys=True).encode()).hexdigest()
try:
    response=session.get(origin+live._API+'/v1/currentuser',timeout=(5,8),allow_redirects=False)
    body=response.json() if response.status_code==200 else {}
    report['identity_matches']=body.get('ok') is True and str((body.get('data') or {}).get('userCode'))==user and creds.get('skl_username')==user
finally:
    session.cookies.clear();session.headers.pop('jwt-token',None);session.close()
if not report['identity_matches']:
    write_json(Path(sys.argv[2]),report);raise SystemExit(2)
with live.LiveClient(cfg['live_url'],cfg) as client:
    client.select_lesson(selected_id,deadline)
    reference=fingerprint(client._session)
    request=client._session.request
    original_get_info=client._get_info
    observed=[]
    def guard(method,url,**kwargs):
        if method.upper()!='GET' or url!=origin+live._API+'/v1/course_vod_videoinfos':
            raise RuntimeError('Unexpected request blocked')
        params=kwargs.get('params') or {}
        observed.append({'course_id':str(params.get('courseId')),'same_auth':fingerprint(client._session)==reference})
        return request(method,url,**kwargs)
    client._session.request=guard
    info_seen={}
    def get_info():
        result=original_get_info()
        data=result.get('data')
        info_seen.clear()
        info_seen['api_status']=result.get('status')
        if isinstance(data,dict):
            info_seen.update(response_course_id=str(data.get('id','')),response_title=data.get('courName') or data.get('subjName') or '',response_class_id=str(data.get('teclId','')))
            endpoints=[]
            for view in data.get('courseDeviceViewDtoList') or []:
                if not isinstance(view,dict):continue
                candidate=view.get('chanNameMainPlayUrl')
                if not isinstance(candidate,str) or not candidate:continue
                parsed=urlsplit(candidate)
                if parsed.scheme in ('http','https') and parsed.hostname:
                    endpoints.append(hashlib.sha256((parsed.scheme+'://'+parsed.netloc+parsed.path).encode()).hexdigest())
            info_seen['media_endpoint_fingerprints']=endpoints
        return result
    client._get_info=get_info
    stream=None
    for index in range(3):
        try:
            stream=client.resolve()
            outcome='resolved'
        except live.LiveNotStarted:
            outcome='not_started'
        except live.LiveEnded:
            outcome='ended'
        except live.LiveError as error:
            outcome=type(error).__name__
        attempt={'outcome':outcome,'auth_still_own':fingerprint(client._session)==reference,**info_seen}
        if stream is not None:
            attempt['stream_fingerprint']=hashlib.sha256(stream.url.encode()).hexdigest()
        report['attempts'].append(attempt)
        if stream is not None:break
        if index<2:time.sleep(1)
    report['requests']=observed
    report['routing_matches']=all(r['course_id']==selected_id and r['same_auth'] for r in observed)
    report['response_id_matches']=all(a.get('response_course_id') in (None,'',selected_id) for a in report['attempts'])
    if stream is not None:
        decoder=ROOT/'_internal/ffmpeg/ffmpeg.exe'
        source=FFmpegAudioSource(stream.url,headers=stream.headers,chunk_seconds=.25,executable=str(decoder))
        stop=DeadlineStop(threading.Event(),time.time()+8)
        try:
            with closing(source.chunks(stop)) as chunks:
                for chunk in chunks:
                    report['audio_seconds']+=len(chunk)/16000
                    if report['audio_seconds']>=3:break
        except MediaError as error:
            report['audio_error_kind']=error.kind
        report['audio_seconds']=round(report['audio_seconds'],2)
    report['auth_after_sampling_matches']=fingerprint(client._session)==reference
report['ok']=report['identity_matches'] and report['routing_matches'] and report['response_id_matches'] and report['auth_after_sampling_matches']
write_json(Path(sys.argv[2]),report)
print(json.dumps(report,ensure_ascii=True),flush=True)
