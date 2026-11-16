import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session as DefaultSession
from src.models.entities import Segment,IdentityBatch
from src.services.analysis import cluster
from src.services.identity import build_identity_result
QUEUE='reader:segments'
IDENTITY_QUEUE='reader:identities'
ARTIFACT_DIR=Path(os.getenv('ARTIFACT_DIR','/artifacts'))
def write_artifact(name,payload):
 try:
  ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
  (ARTIFACT_DIR/name).write_text(json.dumps(payload,ensure_ascii=False))
 except OSError:logging.exception('artifact %s',name)
def recover(client,Session=DefaultSession):
 with Session() as recovery:
  for pending in recovery.query(IdentityBatch).filter(IdentityBatch.state.in_(['queued','running'])).all():
   pending.state='queued';client.rpush(IDENTITY_QUEUE,pending.id)
  for pending in recovery.query(Segment).filter(Segment.state.in_(['queued','running'])).all():
   pending.state='queued';client.rpush(QUEUE,pending.id)
  recovery.commit()
def claim(s,model,item):
 """行锁内把 queued 置为 running；已被其他投递认领则返回 None。

 单 worker 是部署前提，这里仍加一层数据库行锁，
 保证同一批次被重复投递时只有一次进入计算，重试不会重复累加。
 """
 row=s.query(model).filter(model.id==int(item)).with_for_update().first()
 if not row or row.state!='queued':
  s.rollback();return None
 row.state='running'
 if model is IdentityBatch:row.attempts=(row.attempts or 0)+1
 s.commit();return row
def run_identity_batch(client,item,Session=DefaultSession):
 with Session() as s:
  row=claim(s,IdentityBatch,item)
  if not row:return
  try:
   # 结果由冻结快照+关系一次性重建；状态与结果同一事务提交，重试不会累加。
   result=build_identity_result(row.snapshot or [],row.relations or [],batch_id=row.id,name=row.name)
   row.result=result;row.checksum=result['checksum'];row.state='done';row.error='';s.commit()
   write_artifact('identity-'+str(row.id)+'.json',result)
  except Exception as e:
   s.rollback()
   row=s.get(IdentityBatch,int(item))
   if row:row.state='failed';row.error=str(e);s.commit()
   logging.exception('identity batch %s',item)
def run_segment(client,item,Session=DefaultSession):
 with Session() as s:
  row=claim(s,Segment,item)
  if not row:return
  snapshot=row.result['snapshot']
  try:
   identity={'batch_id':row.identity_batch_id,'version':row.identity_version,'checksum':row.identity_checksum}
   row.result={**cluster(snapshot,row.features,row.k,identity),'snapshot':snapshot};row.state='done'
   write_artifact(str(row.id)+'.json',row.result)
  except Exception as e:row.state='failed';row.error=str(e)
  s.commit()
def main():
 r=redis.Redis.from_url(os.environ['REDIS_URL'])
 recover(r)
 while True:
  Path('/tmp/heartbeat').touch()
  try:
   item=r.blpop([IDENTITY_QUEUE,QUEUE],timeout=3)
   if not item:continue
   queue,value=item
   if queue.decode()==IDENTITY_QUEUE:run_identity_batch(r,value)
   else:run_segment(r,value)
  except Exception:logging.exception('worker');time.sleep(2)
if __name__=='__main__':main()
