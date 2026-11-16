import os,json
from datetime import datetime,timezone
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Reader,IdentityBatch,Segment,Outreach,Audit
from src.schemas.business import Login,ReaderInput,SegmentInput,IdentityBatchInput,OutreachInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.identity import normalize_relations,dedupe_relations,reader_rows,load_identity_version,identity_cluster_rows,resolve_outreach
from src.controllers.business import ensure_segment
router=APIRouter()
QUEUE='reader:segments'
IDENTITY_QUEUE='reader:identities'
def push(queue,ident):
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush(queue,ident)
 except redis.RedisError:raise HTTPException(503,'队列暂不可用，请稍后重试')
def ensure_batch(s,i):
 row=s.get(IdentityBatch,i)
 if not row:raise HTTPException(404,'身份归并批次不存在')
 return row
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/readers')
def readers(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Reader).order_by(Reader.id).all()]
@router.post('/readers')
def create(data:ReaderInput,u=Depends(editor),s=Depends(db)):
 r=Reader(**data.model_dump());s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'会员编号重复')
 audit(s,u,'录入阅读档案 '+r.code);return dump(r)
@router.put('/readers/{i}')
def edit(i:int,data:ReaderInput,u=Depends(editor),s=Depends(db)):
 r=s.get(Reader,i)
 if not r:raise HTTPException(404,'会员档案不存在')
 for k,v in data.model_dump().items():setattr(r,k,v)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'会员编号重复')
 audit(s,u,'修订档案 '+str(i));return dump(r)
@router.get('/identity-batches')
def identity_batches(u=Depends(current),s=Depends(db)):
 out=[]
 for r in s.query(IdentityBatch).order_by(IdentityBatch.id.desc()).all():
  item=dump(r);item.pop('snapshot',None);item.pop('result',None)
  item['stats']=(r.result or {}).get('stats');item['identity_count']=len((r.result or {}).get('identities',[])) if r.result else None
  out.append(item)
 return out
@router.post('/identity-batches')
def create_identity_batch(data:IdentityBatchInput,u=Depends(editor),s=Depends(db)):
 try:edges=normalize_relations(data.relations);duplicates=dedupe_relations(data.relations)[1]
 except ValueError as e:raise HTTPException(422,str(e))
 snapshot=reader_rows(s)
 batch=IdentityBatch(name=data.name,state='queued',relations=edges,snapshot=snapshot,created=datetime.now(timezone.utc).isoformat(),created_by=u.email,attempts=0)
 s.add(batch);s.commit()
 try:push(IDENTITY_QUEUE,batch.id)
 except HTTPException:
  batch.state='failed';batch.error='队列暂不可用，请稍后重试';s.commit();raise
 audit(s,u,'提交身份归并批次 '+str(batch.id)+f'（{len(edges)}条关系，忽略{duplicates}条重复边）')
 return dump(batch)
@router.get('/identity-batches/{i}')
def identity_batch(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_batch(s,i);item=dump(r);item.pop('snapshot',None);return item
@router.get('/identity-batches/{i}/export')
def export_identity_batch(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_batch(s,i)
 if r.state!='done':raise HTTPException(409,'仅已生效归并批次可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="identity-batch-{i}.json"'})
@router.get('/identities/current')
def current_identities(u=Depends(current),s=Depends(db)):
 v=load_identity_version(s);return {'batch_id':v['batch_id'],'version':v['version'],'name':v['name'],'state':v['state'],**v['result']}
@router.get('/segments')
def segments(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Segment).order_by(Segment.id.desc()).all()]
@router.post('/segments')
def train(data:SegmentInput,u=Depends(editor),s=Depends(db)):
 version=load_identity_version(s)
 rows=identity_cluster_rows(version)
 r=Segment(**data.model_dump(),result={'snapshot':rows},identity_batch_id=version['batch_id'],identity_version=version['version'],identity_checksum=version['result']['checksum']);s.add(r);s.commit()
 try:push(QUEUE,r.id)
 except HTTPException:
  r.state='failed';r.error='队列暂不可用，请稍后重试';s.commit();raise
 audit(s,u,f"提交分群 {r.id}（身份版本 #{version['version']}，{len(rows)}个身份）");return dump(r)
@router.get('/segments/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_segment(s,i))
@router.get('/segments/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_segment(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功分群可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="reader-segments-{i}.json"'})
@router.get('/outreaches')
def outreaches(u=Depends(current),s=Depends(db)):
 version=load_identity_version(s)
 out=[]
 for r in s.query(Outreach).order_by(Outreach.id.desc()).all():
  item=dump(r)
  # 已签核方案名单冻结；草案额外给出“按当前身份版本”的实时校验，仅展示不落库。
  item['current_identity_version']=version['version']
  item['stale']=r.status=='draft' and r.identity_version!=version['version']
  item['live_conflicts']=[] if r.status!='draft' else resolve_outreach(ensure_segment(s,r.segment_id),r.cluster_id,version)['conflicts']
  out.append(item)
 return out
@router.post('/outreaches')
def outreach(data:OutreachInput,u=Depends(editor),s=Depends(db)):
 r=ensure_segment(s,data.segment_id)
 if r.state!='done':raise HTTPException(409,'分群完成后才能制定触达方案')
 raw=[m['code'] for m in r.result.get('members',[]) if m['cluster_id']==data.cluster_id]
 if not raw:raise HTTPException(422,'该版本没有所选群组')
 version=load_identity_version(s)
 resolved=resolve_outreach(r,data.cluster_id,version)
 o=Outreach(**data.model_dump(),members=resolved['members'],identity_batch_id=version['batch_id'],identity_version=version['version'],identity_checksum=version['result']['checksum'],member_details=resolved['member_details'],conflicts=resolved['conflicts'],approval_identity_checksum='');s.add(o);s.commit()
 audit(s,u,f"起草触达方案 {o.id}（身份版本 #{version['version']}，{resolved['raw_codes']}个原始编号→{resolved['unique_identities']}个身份，{len(resolved['conflicts'])}项冲突）");return dump(o)
@router.post('/outreaches/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=s.get(Outreach,i)
 if not r:raise HTTPException(404,'触达方案不存在')
 if r.status!='draft':raise HTTPException(409,'已签核方案不可重复签核')
 # 签核本身只读取一次完整身份版本；冲突和名单漂移都在同一个版本上判断。
 version=load_identity_version(s)
 resolved=resolve_outreach(ensure_segment(s,r.segment_id),r.cluster_id,version)
 if resolved['conflicts']:
  r.conflicts=resolved['conflicts'];s.commit()
  raise HTTPException(409,'当前身份版本存在冲突，不能签核：'+resolved['conflicts'][0]['reason'])
 if set(resolved['members'])!=set(r.members or []):
  r.conflicts=[{'kind':'identity_drift','identity':m,'source_codes':[m],'clusters':[r.cluster_id],'reason':f'身份版本 #{version["version"]} 下成员名单与草案不一致，请重新起草后再签核'} for m in sorted(set(resolved['members'])^set(r.members or []))[:20]]
  s.commit()
  raise HTTPException(409,'身份版本已变化，草案名单需要重新核对后再签核')
 r.status='approved'
 r.member_details=resolved['member_details']
 r.conflicts=[]
 r.members=resolved['members']
 r.identity_batch_id=version['batch_id'];r.identity_version=version['version'];r.identity_checksum=version['result']['checksum']
 r.approval_identity_batch_id=version['batch_id'];r.approval_identity_version=version['version'];r.approval_identity_checksum=version['result']['checksum']
 r.approved_at=datetime.now(timezone.utc).isoformat()
 s.commit();audit(s,u,f"签核触达方案 {i}（身份版本 #{version['version']}，{len(r.members)}个身份）");return dump(r)
@router.get('/outreaches/{i}/export')
def export_outreach(i:int,u=Depends(current),s=Depends(db)):
 r=s.get(Outreach,i)
 if not r:raise HTTPException(404,'触达方案不存在')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="reader-outreach-{i}.json"'})
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
