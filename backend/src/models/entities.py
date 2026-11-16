from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class Reader(Base):
 __tablename__='readers'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);weekly_reading=Column(Float);completion=Column(Float);night_share=Column(Float);note=Column(Text,default='')
class IdentityBatch(Base):
 __tablename__='identity_batches'
 id=Column(Integer,primary_key=True);name=Column(String(100));state=Column(String(20),default='queued');relations=Column(JSON);snapshot=Column(JSON);result=Column(JSON);error=Column(Text,default='');checksum=Column(String(64),default='');attempts=Column(Integer,default=0);created=Column(String(40),default='');created_by=Column(String(100),default='')
class Segment(Base):
 __tablename__='segments'
 id=Column(Integer,primary_key=True);name=Column(String(100));k=Column(Integer);features=Column(JSON);state=Column(String(20),default='queued');result=Column(JSON);error=Column(Text,default='');identity_batch_id=Column(Integer);identity_version=Column(Integer,default=0);identity_checksum=Column(String(64),default='')
class Outreach(Base):
 __tablename__='outreaches'
 id=Column(Integer,primary_key=True);segment_id=Column(Integer,ForeignKey('segments.id'));cluster_id=Column(Integer);title=Column(String(100));note=Column(Text);members=Column(JSON);status=Column(String(20),default='draft');identity_batch_id=Column(Integer);identity_version=Column(Integer,default=0);identity_checksum=Column(String(64),default='');member_details=Column(JSON);conflicts=Column(JSON);approval_identity_batch_id=Column(Integer);approval_identity_version=Column(Integer);approval_identity_checksum=Column(String(64),default='');approved_at=Column(String(40),default='')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
