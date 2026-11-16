import json,hashlib,math
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
FEATURES=['weekly_reading','completion','night_share']
SEED=3107
def cluster(rows,features,k,identity=None):
 if len(features)<2 or len(set(features))!=len(features) or not set(features)<=set(FEATURES):raise ValueError('选择2–3个不同的有效特征')
 if k<2 or k>6:raise ValueError('分群数仅支持2–6')
 if len(rows)<=k:raise ValueError('样本数必须大于分群数，补充会员档案或降低k')
 missing=[r.get('code') for r in rows for f in features if r.get(f) is None or not math.isfinite(float(r[f]))]
 if missing:raise ValueError('身份 '+ '、'.join(sorted(set(missing))[:5]) +f' 等{len(set(missing))}个在所选特征上缺少有效值（零篇数身份的比例未定义），请改选特征或修订档案；不静默补值')
 x=np.array([[r[f] for f in features] for r in rows],dtype=float)
 if not np.isfinite(x).all():raise ValueError('特征包含缺失或非有限值，请先修订档案')
 if np.any(np.ptp(x,axis=0)<=1e-12):raise ValueError('所选特征中存在常量列，请移除该特征或补充有差异的样本')
 if len(np.unique(x,axis=0))<k:raise ValueError('不同特征点少于k，不能形成指定数量的群组')
 scaler=StandardScaler();z=scaler.fit_transform(x);model=KMeans(n_clusters=k,random_state=SEED,n_init=20,algorithm='lloyd');labels=model.fit_predict(z)
 if len(set(labels))!=k:raise ValueError('实际群数小于k，请检查重复点或降低k')
 centers=scaler.inverse_transform(model.cluster_centers_)
 order=sorted(range(k),key=lambda i:tuple(centers[i]));mapping={old:new for new,old in enumerate(order)}
 memberships=[dict(id=r['id'],code=r['code'],identity_key=r.get('identity_key',r['code']),source_codes=list(r.get('source_codes') or [r['code']]),identity_size=r.get('size',1),merge_basis=list(r.get('merge_basis') or []),zero_articles=bool(r.get('zero_articles')),cluster_id=mapping[int(label)],**{f:r[f] for f in FEATURES},x=float(point[0]),y=float(point[1])) for r,label,point in zip(rows,labels,z)]
 profiles=[]
 for old in order:
  members=[r for r in memberships if r['cluster_id']==mapping[old]]
  portrait={}
  for f in FEATURES:
   vals=[r[f] for r in members if r[f] is not None]
   portrait[f]=float(np.mean(vals)) if vals else None
  profiles.append(dict(cluster_id=mapping[old],size=len(members),source_code_count=sum(r['identity_size'] for r in members),portrait=portrait,center_inverse={f:float(v) for f,v in zip(features,centers[old])},member_codes=[r['code'] for r in members]))
 result=dict(n=len(rows),k=k,features=features,profiles=profiles,members=memberships,silhouette=float(silhouette_score(z,labels)),inertia=float(model.inertia_),scaler_mean=scaler.mean_.tolist(),scaler_scale=scaler.scale_.tolist(),seed=SEED,checksum=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),method='每个当前身份只取一份合并档案；StandardScaler总体均值/标准差→KMeans欧氏距离，n_init=20、seed=3107；群号按原始中心排序，便于同一版本复核；轮廓系数用标准化空间。分群是行为相似性，不是会员价值或因果判断。')
 if identity is not None:result['identity']=identity
 return result
