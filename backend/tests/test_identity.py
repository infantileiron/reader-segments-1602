"""后端测试使用 SQLite 与内存队列，验证身份归并口径和并发一致性。

真实部署仍使用 PostgreSQL/Redis；测试不改动生产路径，只替换连接与队列。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = Path(tempfile.mkdtemp()) / 'test.db'
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(DB_PATH))
os.environ.setdefault('JWT_SECRET', 'test-secret-3107')
os.environ.setdefault('REDIS_URL', 'redis://unused:6379/0')

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.models import entities
from src.models.entities import User, Reader, IdentityBatch, Segment, Outreach
from src.routes import api
from src.services import identity
from src.services.identity import build_identity_result, resolve_outreach


class FakeQueue:
    def __init__(self):
        self.items = []

    def rpush(self, queue, value):
        self.items.append((queue, value))

    def blpop(self, queues, timeout=0):
        for index, item in enumerate(self.items):
            if item[0] in queues:
                return self.items.pop(index)[0].encode(), str(item[1]).encode()
        return None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as s:
            yield s

    from src.main import app as fastapi_app
    fastapi_app.dependency_overrides[api.db] = override_db
    queue = FakeQueue()
    monkeypatch.setattr(api, 'push', lambda q, i: queue.rpush(q, i))
    # 产物目录改到临时目录，避免测试写 /artifacts
    from src import worker
    monkeypatch.setattr(worker, 'ARTIFACT_DIR', tmp_path)
    # 种子账号
    from src.utils.security import hash_password
    with Session() as s:
        for role in ('admin', 'analyst', 'viewer'):
            s.add(User(email=role + '@demo.local', role=role, password=hash_password('Demo123!')))
        s.commit()
    client = TestClient(fastapi_app)
    client.queue = queue
    client.session = Session
    yield client
    fastapi_app.dependency_overrides.clear()


def login(client, role='analyst'):
    r = client.post('/auth/login', json={'email': role + '@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def add_reader(client, headers, code, weekly, completion, night):
    r = client.post('/readers', json={'code': code, 'weekly_reading': weekly, 'completion': completion, 'night_share': night, 'note': ''}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def run_batch(client, batch_id):
    """模拟 worker 处理一个批次，返回处理后的记录。"""
    from src.worker import run_identity_batch
    run_identity_batch(client.queue, str(batch_id).encode(), client.session)
    with client.session() as s:
        return s.get(IdentityBatch, batch_id)


def run_segment(client, segment_id):
    from src.worker import run_segment
    run_segment(client.queue, str(segment_id).encode(), client.session)
    with client.session() as s:
        return s.get(Segment, segment_id)


# ---------- 纯函数口径 ----------

def test_chain_merge_sums_articles_and_weights_ratios():
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': 10, 'completion': 0.5, 'night_share': 0.2},
        {'id': 2, 'code': 'B', 'weekly_reading': 30, 'completion': 0.9, 'night_share': 0.6},
        {'id': 3, 'code': 'C', 'weekly_reading': 60, 'completion': 0.7, 'night_share': 0.3},
    ]
    relations = [{'code_a': 'A', 'code_b': 'B', 'basis': '手机号'}, {'code_a': 'B', 'code_b': 'C', 'basis': '邮箱'}]
    result = build_identity_result(readers, relations, batch_id=1)
    assert len(result['identities']) == 1
    merged = result['identities'][0]
    assert merged['source_codes'] == ['A', 'B', 'C']
    assert merged['weekly_reading'] == 100
    assert abs(merged['completion'] - (10 * .5 + 30 * .9 + 60 * .7) / 100) < 1e-9
    assert abs(merged['night_share'] - (10 * .2 + 30 * .6 + 60 * .3) / 100) < 1e-9
    assert merged['zero_articles'] is False
    assert result['stats']['reader_codes'] == 3
    assert result['stats']['identities'] == 1


def test_duplicate_and_reversed_edges_counted_once():
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': 10, 'completion': 0.5, 'night_share': 0.2},
        {'id': 2, 'code': 'B', 'weekly_reading': 30, 'completion': 0.9, 'night_share': 0.6},
    ]
    relations = [
        {'code_a': 'A', 'code_b': 'B', 'basis': '手机号'},
        {'code_a': 'B', 'code_b': 'A', 'basis': '手机号'},
        {'code_a': 'A', 'code_b': 'B', 'basis': '手机号'},
    ]
    result = build_identity_result(readers, relations, batch_id=1)
    assert result['stats']['relations'] == 1
    assert result['stats']['duplicate_edges'] == 2
    assert result['identities'][0]['weekly_reading'] == 40
    assert len(result['identities'][0]['merge_basis']) == 1


def test_self_loop_rejected():
    with pytest.raises(ValueError):
        build_identity_result([], [{'code_a': 'A', 'code_b': 'A', 'basis': 'x'}], batch_id=1)


def test_zero_total_articles_keeps_ratio_undefined():
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': 0, 'completion': 0.2, 'night_share': 0.1},
        {'id': 2, 'code': 'B', 'weekly_reading': 0, 'completion': 0.7, 'night_share': 0.3},
    ]
    result = build_identity_result(readers, [{'code_a': 'A', 'code_b': 'B', 'basis': 'x'}], batch_id=1)
    merged = result['identities'][0]
    assert merged['weekly_reading'] == 0
    assert merged['completion'] is None and merged['night_share'] is None
    assert merged['zero_articles'] is True
    assert result['stats']['zero_article_identities'] == 1


def test_unknown_code_quarantines_whole_group():
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': 10, 'completion': 0.5, 'night_share': 0.2},
        {'id': 2, 'code': 'B', 'weekly_reading': 20, 'completion': 0.6, 'night_share': 0.3},
    ]
    relations = [{'code_a': 'A', 'code_b': 'ZZZ', 'basis': '手机号'}]
    result = build_identity_result(readers, relations, batch_id=1)
    assert len(result['quarantined']) == 1
    group = result['quarantined'][0]
    assert sorted(group['source_codes']) == ['A', 'ZZZ']
    assert group['weekly_reading'] is None and group['completion'] is None
    assert 'ZZZ' in group['quarantine_reason']
    assert result['stats']['quarantined_groups'] == 1
    # B 未参与关系，仍是独立身份
    assert [x['canonical_code'] for x in result['identities']] == ['B']


def test_original_readers_unchanged_after_merge():
    readers = [{'id': 1, 'code': 'A', 'weekly_reading': 10, 'completion': 0.5, 'night_share': 0.2, 'note': '原档'}]
    result = build_identity_result(readers, [], batch_id=1)
    assert result['identities'][0]['source_codes'] == ['A']
    assert readers[0]['weekly_reading'] == 10 and readers[0]['note'] == '原档'


def test_resolve_outreach_dedupes_and_flags_cross_cluster():
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': 10, 'completion': 0.5, 'night_share': 0.2},
        {'id': 2, 'code': 'B', 'weekly_reading': 30, 'completion': 0.9, 'night_share': 0.6},
    ]
    version = {'result': build_identity_result(readers, [{'code_a': 'A', 'code_b': 'B', 'basis': 'x'}], batch_id=1)}
    segment = type('S', (), {'result': {'members': [
        {'code': 'A', 'cluster_id': 0, 'source_codes': ['A']},
        {'code': 'B', 'cluster_id': 1, 'source_codes': ['B']},
    ]}})()
    resolved = resolve_outreach(segment, 1, version)
    assert resolved['members'] == ['A']
    assert resolved['unique_identities'] == 1
    assert resolved['raw_codes'] == 2
    assert any(c['kind'] == 'cross_cluster' for c in resolved['conflicts'])


# ---------- HTTP 流程 ----------

def test_batch_completion_applies_atomically_and_is_idempotent(app):
    analyst = login(app, 'analyst')
    add_reader(app, analyst, 'SL-001', 10, 0.5, 0.2)
    add_reader(app, analyst, 'SL-002', 30, 0.9, 0.6)

    r = app.post('/identity-batches', json={'name': '测试归并', 'relations': [
        {'code_a': 'SL-001', 'code_b': 'SL-002', 'basis': '手机号'},
        {'code_a': 'SL-002', 'code_b': 'SL-001', 'basis': '手机号'},
    ]}, headers=analyst)
    assert r.status_code == 200, r.text
    batch_id = r.json()['id']
    assert r.json()['state'] == 'queued'

    # 未完成前当前身份仍是基础身份，不部分生效
    current = app.get('/identities/current', headers=analyst).json()
    assert current['version'] == 0
    assert current['stats']['identities'] == 2
    assert current['stats']['merged_identities'] == 0

    done = run_batch(app, batch_id)
    assert done.state == 'done'
    assert done.result['stats']['identities'] == 1
    checksum = done.checksum

    # 重复投递同一批次：worker 看到非 queued 直接跳过，篇数不重复累加
    app.queue.rpush('reader:identities', batch_id)
    from src.worker import run_identity_batch
    run_identity_batch(app.queue, str(batch_id).encode(), app.session)
    with app.session() as s:
        again = s.get(IdentityBatch, batch_id)
        assert again.result['identities'][0]['weekly_reading'] == 40
        assert again.checksum == checksum

    current = app.get('/identities/current', headers=analyst).json()
    assert current['version'] == batch_id
    assert current['stats']['reader_codes'] == 2
    assert current['identities'][0]['weekly_reading'] == 40

    # 原始档案保留，篇数没有被改写
    rows = app.get('/readers', headers=analyst).json()
    assert [row['weekly_reading'] for row in rows] == [10, 30]


def test_segment_freezes_identity_version_and_uses_one_row_per_identity(app):
    analyst = login(app, 'analyst')
    for i in range(1, 9):
        add_reader(app, analyst, f'SL-{i:03}', i * 2, 0.2 + i * 0.05, 0.1 + i * 0.03)
    r = app.post('/identity-batches', json={'name': '归并', 'relations': [
        {'code_a': 'SL-001', 'code_b': 'SL-002', 'basis': '手机号'},
        {'code_a': 'SL-002', 'code_b': 'SL-003', 'basis': '邮箱'},
    ]}, headers=analyst)
    batch_id = r.json()['id']
    run_batch(app, batch_id)

    seg = app.post('/segments', json={'name': '身份分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    assert seg.status_code == 200, seg.text
    body = seg.json()
    assert body['identity_batch_id'] == batch_id
    assert body['identity_version'] == batch_id
    assert len(body['result']['snapshot']) == 6  # 8 编号 → 3 合1 + 5 = 6 身份

    done = run_segment(app, body['id'])
    assert done.state == 'done', done.error
    assert done.result['n'] == 6
    merged = [m for m in done.result['members'] if m['code'] == 'SL-001'][0]
    assert merged['source_codes'] == ['SL-001', 'SL-002', 'SL-003']
    assert merged['weekly_reading'] == 2 + 4 + 6

    # 后续新增归并批次不影响已冻结的分群版本
    app.post('/identity-batches', json={'name': '后来归并', 'relations': [
        {'code_a': 'SL-004', 'code_b': 'SL-005', 'basis': '手机号'},
    ]}, headers=analyst)
    detail = app.get(f"/segments/{body['id']}", headers=analyst).json()
    assert detail['identity_version'] == batch_id
    assert len(detail['result']['snapshot']) == 6


def test_zero_article_identity_fails_feature_instead_of_faking(app):
    analyst = login(app, 'analyst')
    for i in range(1, 6):
        add_reader(app, analyst, f'Z-{i:02}', i, 0.3, 0.1)
    add_reader(app, analyst, 'Z-06', 0, 0.4, 0.2)
    r = app.post('/identity-batches', json={'name': '零篇', 'relations': [
        {'code_a': 'Z-01', 'code_b': 'Z-02', 'basis': 'x'},
    ]}, headers=analyst)
    run_batch(app, r.json()['id'])
    seg = app.post('/segments', json={'name': '零篇分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    assert seg.status_code == 200
    done = run_segment(app, seg.json()['id'])
    assert done.state == 'failed'
    assert '零篇数' in done.error or '缺少有效值' in done.error


def test_outreach_dedupes_conflicts_block_approval_and_approval_freezes(app):
    analyst = login(app, 'analyst')
    admin = login(app, 'admin')
    for i in range(1, 13):
        add_reader(app, analyst, f'O-{i:02}', i * 3, 0.2 + (i % 5) * 0.1, 0.1 + (i % 4) * 0.1)
    # A(O-01)-B(O-02) 同人；C(O-03)-D(O-04) 同人
    r = app.post('/identity-batches', json={'name': '同人', 'relations': [
        {'code_a': 'O-01', 'code_b': 'O-02', 'basis': '手机号'},
        {'code_a': 'O-03', 'code_b': 'O-04', 'basis': '邮箱'},
    ]}, headers=analyst)
    run_batch(app, r.json()['id'])

    seg = app.post('/segments', json={'name': '触达分群', 'k': 2, 'features': ['weekly_reading', 'night_share']}, headers=analyst)
    run_segment(app, seg.json()['id'])
    detail = app.get(f"/segments/{seg.json()['id']}", headers=analyst).json()
    members = detail['result']['members']
    identity_cluster = {m['code']: m['cluster_id'] for m in members}

    # 找到两个群组，各有成员
    clusters = sorted({m['cluster_id'] for m in members})
    cluster_a, cluster_b = clusters[0], clusters[1]
    first = [m for m in members if m['cluster_id'] == cluster_a][0]
    second = [m for m in members if m['cluster_id'] == cluster_b][0]

    o1 = app.post('/outreaches', json={'segment_id': seg.json()['id'], 'cluster_id': first['cluster_id'], 'title': '群A方案', 'note': '每周一次短篇精选'}, headers=analyst)
    assert o1.status_code == 200, o1.text
    assert o1.json()['conflicts'] == []
    # 群A包含合并身份时，成员按身份去重
    for detail_member in o1.json()['member_details']:
        assert detail_member['canonical_code'] not in detail_member['source_codes'][1:]
    assert len(set(o1.json()['members'])) == len(o1.json()['members'])

    # 先签核无冲突方案，冻结名单
    approved = app.post(f"/outreaches/{o1.json()['id']}/approve", json={}, headers=admin)
    assert approved.status_code == 200, approved.text
    frozen = approved.json()['members']

    # 构造冲突：把一个已属于群B的身份来源编号也挂进群A（模拟旧分群跨群同人）
    with app.session() as s:
        seg_row = s.get(Segment, seg.json()['id'])
        result = dict(seg_row.result)
        result['members'] = [dict(m) for m in result['members']]
        target = [m for m in result['members'] if m['cluster_id'] == cluster_b][0]
        result['members'].append({'code': target['code'], 'cluster_id': cluster_a, 'source_codes': list(target['source_codes'])})
        seg_row.result = result
        s.commit()

    o2 = app.post('/outreaches', json={'segment_id': seg.json()['id'], 'cluster_id': cluster_a, 'title': '冲突方案', 'note': '这版存在跨群同人需要阻止'}, headers=analyst)
    assert o2.status_code == 200
    kinds = {c['kind'] for c in o2.json()['conflicts']}
    assert 'cross_cluster' in kinds, o2.json()['conflicts']
    blocked = app.post(f"/outreaches/{o2.json()['id']}/approve", json={}, headers=admin)
    assert blocked.status_code == 409
    assert '冲突' in blocked.json()['detail']

    # 已签核方案保留原名单：签核后再加新归并批次，名单不漂移
    fresh = app.post('/identity-batches', json={'name': '签核后归并', 'relations': [
        {'code_a': first['code'], 'code_b': second['code'], 'basis': '手机号'},
    ]}, headers=analyst)
    run_batch(app, fresh.json()['id'])
    exported = app.get(f"/outreaches/{o1.json()['id']}/export", headers=admin).json()
    assert exported['members'] == frozen
    assert exported['status'] == 'approved'
    assert exported['approval_identity_version'] == o1.json()['identity_version']


def test_duplicate_edges_reported_in_batch(app):
    analyst = login(app, 'analyst')
    add_reader(app, analyst, 'D-01', 5, 0.5, 0.2)
    add_reader(app, analyst, 'D-02', 6, 0.6, 0.3)
    r = app.post('/identity-batches', json={'name': '重复边', 'relations': [
        {'code_a': 'D-01', 'code_b': 'D-02', 'basis': '手机号'},
        {'code_a': 'D-02', 'code_b': 'D-01', 'basis': '手机号'},
        {'code_a': 'D-01', 'code_b': 'D-02', 'basis': '同一设备'},
    ]}, headers=analyst)
    assert r.status_code == 200
    done = run_batch(app, r.json()['id'])
    assert done.result['stats']['relations'] == 1
    assert done.result['stats']['duplicate_edges'] == 2
    assert done.result['identities'][0]['weekly_reading'] == 11


def test_self_loop_rejected_by_api(app):
    analyst = login(app, 'analyst')
    r = app.post('/identity-batches', json={'name': '自环', 'relations': [
        {'code_a': 'X', 'code_b': 'X', 'basis': '错误'},
    ]}, headers=analyst)
    assert r.status_code == 422


def test_viewer_cannot_create_batch(app):
    viewer = login(app, 'viewer')
    r = app.post('/identity-batches', json={'name': '越权', 'relations': [
        {'code_a': 'A', 'code_b': 'B', 'basis': 'x'},
    ]}, headers=viewer)
    assert r.status_code == 403


def test_batch_uses_frozen_snapshot_despite_concurrent_reader_edit(app):
    analyst = login(app, 'analyst')
    add_reader(app, analyst, 'C-01', 10, 0.5, 0.2)
    add_reader(app, analyst, 'C-02', 20, 0.6, 0.3)
    r = app.post('/identity-batches', json={'name': '冻结快照', 'relations': [
        {'code_a': 'C-01', 'code_b': 'C-02', 'basis': '手机号'},
    ]}, headers=analyst)
    batch_id = r.json()['id']
    # 批次排队期间修订档案：批次仍使用创建时的快照，归并结果不被半途改写
    rows = app.get('/readers', headers=analyst).json()
    target = [x for x in rows if x['code'] == 'C-01'][0]
    app.put(f"/readers/{target['id']}", json={'code': 'C-01', 'weekly_reading': 999, 'completion': 0.1, 'night_share': 0.9, 'note': '修订'}, headers=analyst)
    done = run_batch(app, batch_id)
    assert done.result['identities'][0]['weekly_reading'] == 30
    # 修订后的档案仍保留且可用于后续批次
    rows = app.get('/readers', headers=analyst).json()
    assert [x['weekly_reading'] for x in rows] == [999, 20]


def test_interrupted_batch_retry_does_not_double_count(app):
    analyst = login(app, 'analyst')
    add_reader(app, analyst, 'R-01', 10, 0.5, 0.2)
    add_reader(app, analyst, 'R-02', 20, 0.6, 0.3)
    r = app.post('/identity-batches', json={'name': '重试', 'relations': [
        {'code_a': 'R-01', 'code_b': 'R-02', 'basis': '手机号'},
    ]}, headers=analyst)
    batch_id = r.json()['id']
    # 模拟中断：worker 已置 running 但未写结果
    with app.session() as s:
        batch = s.get(IdentityBatch, batch_id)
        batch.state = 'running'
        s.commit()
    from src.worker import recover, run_identity_batch
    recover(app.queue, app.session)
    assert any(item[0] == 'reader:identities' and item[1] == batch_id for item in app.queue.items)
    # 连续处理两次，结果相同且篇数不累加
    run_identity_batch(app.queue, str(batch_id).encode(), app.session)
    run_identity_batch(app.queue, str(batch_id).encode(), app.session)
    with app.session() as s:
        batch = s.get(IdentityBatch, batch_id)
        assert batch.state == 'done'
        assert batch.result['identities'][0]['weekly_reading'] == 30
        assert batch.attempts >= 1


def test_draft_before_new_batch_is_blocked_when_identity_changes(app):
    analyst = login(app, 'analyst')
    admin = login(app, 'admin')
    for i in range(1, 11):
        add_reader(app, analyst, f'P-{i:02}', i * 4, 0.2 + (i % 4) * 0.15, 0.1 + (i % 3) * 0.2)
    seg = app.post('/segments', json={'name': '旧分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    run_segment(app, seg.json()['id'])
    detail = app.get(f"/segments/{seg.json()['id']}", headers=analyst).json()
    cluster = detail['result']['members'][0]['cluster_id']
    o = app.post('/outreaches', json={'segment_id': seg.json()['id'], 'cluster_id': cluster, 'title': '草案', 'note': '草案起草时是基础身份版本'}, headers=analyst)
    assert o.status_code == 200 and o.json()['identity_version'] == 0
    assert o.json()['conflicts'] == []

    # 草案之后出现归并批次：同一分群版本里两个编号变成同一身份，名单会变化
    members = [m for m in detail['result']['members'] if m['cluster_id'] == cluster]
    assert len(members) >= 2
    a, b = members[0]['code'], members[1]['code']
    r = app.post('/identity-batches', json={'name': '草案后归并', 'relations': [
        {'code_a': a, 'code_b': b, 'basis': '手机号'},
    ]}, headers=analyst)
    run_batch(app, r.json()['id'])

    blocked = app.post(f"/outreaches/{o.json()['id']}/approve", json={}, headers=admin)
    assert blocked.status_code == 409
    assert '身份版本' in blocked.json()['detail'] or '冲突' in blocked.json()['detail']
    # 被阻止原因写回草案，页面能展示
    refreshed = [x for x in app.get('/outreaches', headers=analyst).json() if x['id'] == o.json()['id']][0]
    assert refreshed['status'] == 'draft'
    assert refreshed['conflicts']


def test_quarantined_identity_blocks_approval(app):
    analyst = login(app, 'analyst')
    admin = login(app, 'admin')
    for i in range(1, 11):
        add_reader(app, analyst, f'Q-{i:02}', i * 4, 0.2 + (i % 4) * 0.15, 0.1 + (i % 3) * 0.2)
    # 隔离组不影响分群样本，但分群版本里出现的隔离身份要阻止签核
    r = app.post('/identity-batches', json={'name': '隔离', 'relations': [
        {'code_a': 'Q-01', 'code_b': 'MISSING-01', 'basis': '手机号'},
    ]}, headers=analyst)
    run_batch(app, r.json()['id'])
    seg = app.post('/segments', json={'name': '隔离分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    run_segment(app, seg.json()['id'])
    # 人为把隔离身份塞进分群成员，模拟上游异常
    with app.session() as s:
        row = s.get(Segment, seg.json()['id'])
        result = dict(row.result)
        result['members'] = list(result['members']) + [{'code': 'Q-01', 'cluster_id': 0, 'source_codes': ['Q-01'], 'identity_size': 2}]
        row.result = result
        s.commit()
    o = app.post('/outreaches', json={'segment_id': seg.json()['id'], 'cluster_id': 0, 'title': '隔离草案', 'note': '包含隔离身份的草案'}, headers=analyst)
    assert o.status_code == 200
    assert any(c['kind'] == 'quarantined' for c in o.json()['conflicts'])
    blocked = app.post(f"/outreaches/{o.json()['id']}/approve", json={}, headers=admin)
    assert blocked.status_code == 409


def test_segment_uses_single_complete_identity_version(app):
    """分群创建时冻结版本；worker 计算期间出现新归并批次也不混用两个版本。"""
    analyst = login(app, 'analyst')
    for i in range(1, 9):
        add_reader(app, analyst, f'H-{i:02}', i * 3, 0.2 + (i % 4) * 0.15, 0.1 + (i % 3) * 0.2)
    r1 = app.post('/identity-batches', json={'name': '第一版', 'relations': [
        {'code_a': 'H-01', 'code_b': 'H-02', 'basis': '手机号'},
    ]}, headers=analyst)
    run_batch(app, r1.json()['id'])
    seg = app.post('/segments', json={'name': '并发分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    body = seg.json()
    assert len(body['result']['snapshot']) == 7
    # worker 尚未消费时第二版归并完成：分群仍应只按创建时的身份版本计算
    r2 = app.post('/identity-batches', json={'name': '第二版', 'relations': [
        {'code_a': 'H-03', 'code_b': 'H-04', 'basis': '邮箱'},
    ]}, headers=analyst)
    run_batch(app, r2.json()['id'])
    current = app.get('/identities/current', headers=analyst).json()
    assert current['version'] == r2.json()['id']
    done = run_segment(app, body['id'])
    assert done.state == 'done'
    assert done.identity_version == body['identity_version'] == r1.json()['id']
    assert done.result['n'] == 7
    assert done.result['identity']['checksum'] == body['identity_checksum']


def test_reader_created_after_batch_appears_as_single_identity(app):
    analyst = login(app, 'analyst')
    for i in range(1, 7):
        add_reader(app, analyst, f'N-{i:02}', i * 4, 0.2 + (i % 4) * 0.15, 0.1 + (i % 3) * 0.2)
    r = app.post('/identity-batches', json={'name': '批次后新会员', 'relations': [
        {'code_a': 'N-01', 'code_b': 'N-02', 'basis': '手机号'},
    ]}, headers=analyst)
    run_batch(app, r.json()['id'])
    # 批次完成后新录入的编号不属于任何关系，应以单编号身份出现在当前版本，不被漏掉
    add_reader(app, analyst, 'N-07', 15, 0.8, 0.2)
    current = app.get('/identities/current', headers=analyst).json()
    assert current['appended_codes'] == ['N-07']
    assert current['stats']['reader_codes'] == 7
    codes = [x['canonical_code'] for x in current['identities']]
    assert 'N-07' in codes
    seg = app.post('/segments', json={'name': '新会员分群', 'k': 2, 'features': ['weekly_reading', 'completion']}, headers=analyst)
    assert len(seg.json()['result']['snapshot']) == len(codes)


def test_missing_article_count_is_not_treated_as_zero():
    """缺篇数时总篇数未知，比例也不定义；不能把 None 当 0 参与加权。"""
    readers = [
        {'id': 1, 'code': 'A', 'weekly_reading': None, 'completion': 0.5, 'night_share': 0.2},
        {'id': 2, 'code': 'B', 'weekly_reading': 10, 'completion': 0.9, 'night_share': 0.6},
    ]
    result = build_identity_result(readers, [{'code_a': 'A', 'code_b': 'B', 'basis': 'x'}], batch_id=1)
    merged = result['identities'][0]
    assert merged['weekly_reading'] is None
    assert merged['completion'] is None and merged['night_share'] is None
    assert merged['zero_articles'] is False
