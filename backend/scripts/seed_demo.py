import random
from src.config.database import Base,engine,Session
from src.models.entities import User,Reader
from src.utils.security import hash_password
# 演示同人关系：同一会员在不同渠道留下多个编号，含链式、重复边与跨编号环；另有一条未知编号用于展示隔离。
DEMO_RELATIONS=[
 ('SL-003','SL-083','手机号相同'),
 ('SL-083','SL-163','同一会员中心ID'),
 ('SL-163','SL-003','跨渠道又确认一次，构成环但不再新增身份'),
 ('SL-083','SL-003','重复导入的同一条关系'),
 ('SL-017','SL-097','同一设备'),
 ('SL-042','SL-122','邮箱相同'),
 ('SL-211','SL-999','疑似同人，但SL-999尚未建档'),
]
def seed():
 Base.metadata.create_all(engine);rng=random.Random(3107)
 with Session() as s:
  for role in ('admin','analyst','viewer'):
   email=role+'@demo.local'
   if not s.query(User).filter_by(email=email).first():s.add(User(email=email,role=role,password=hash_password('Demo123!')))
  for i in range(240):
   code=f'SL-{i+1:03}';mode=i//80
   reading=round(max(0,rng.gauss([4,14,26][mode],[1.1,3,4][mode])),2)
   completion=round(max(0,min(1,rng.gauss([.32,.81,.61][mode],.06))),4)
   night=round(max(0,min(1,rng.gauss([.18,.23,.77][mode],.08))),4)
   if not s.query(Reader).filter_by(code=code).first():s.add(Reader(code=code,weekly_reading=reading,completion=completion,night_share=night,note='最近完整自然周行为聚合；模拟会员，不含个人联系方式'))
  s.commit();print({'users':s.query(User).count(),'readers':s.query(Reader).count(),'demo_relations':len(DEMO_RELATIONS)});return DEMO_RELATIONS
if __name__=='__main__':seed()
