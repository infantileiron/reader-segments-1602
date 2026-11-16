"""身份归并：按同人关系做传递闭包，聚合原始阅读档案。

口径：
- 原始 readers 档案永不改写；归并结果只存在于批次版本中。
- 周阅读篇数直接相加。
- 完读率、夜读占比按周阅读篇数加权；总篇数为 0 时保持 None，不补 0、不补平均值。
- 连通组含未知编号时整组隔离，不进入新分群。
"""
import hashlib
import json
from collections import defaultdict

FEATURES = ['weekly_reading', 'completion', 'night_share']
SCHEMA = 'identity-merge-1'


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, code):
        self.parent.setdefault(code, code)

    def find(self, code):
        root = code
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[code] != code:
            nxt = self.parent[code]
            self.parent[code] = root
            code = nxt
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


def normalize_relations(relations):
    """校验同人关系并规范化字段，保留重复边。

    去重统计在 build_identity_result 里做，批次保存的是原始关系输入，
    这样 worker 能从冻结快照完整复核“重复边”数量。
    - 自环直接拒绝；
    - 无向边统一为 (a, b) 两个端点，不做方向判断。
    """
    edges = []
    for index, item in enumerate(relations):
        if hasattr(item, 'model_dump'):
            item = item.model_dump()
        left = str(item.get('code_a') or item.get('a') or '').strip()
        right = str(item.get('code_b') or item.get('b') or '').strip()
        basis = str(item.get('basis') or '').strip()
        if not left or not right:
            raise ValueError(f'第{index + 1}条同人关系缺少编号')
        if left == right:
            raise ValueError(f'第{index + 1}条同人关系是自环：{left}')
        edges.append({'a': left, 'b': right, 'basis': basis or '同人关系'})
    return edges


def dedupe_relations(edges):
    """按无向边去重，返回 (去重后的边, 重复边数量)。A-B、B-A、重复提交都只算一条。"""
    seen, unique, duplicates = set(), [], 0
    for edge in normalize_relations(edges):
        key = tuple(sorted((edge['a'], edge['b'])))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(edge)
    return unique, duplicates


def _source_rows(readers):
    return [{
        'id': r.get('id'),
        'code': r['code'],
        'weekly_reading': r.get('weekly_reading'),
        'completion': r.get('completion'),
        'night_share': r.get('night_share'),
        'note': r.get('note', ''),
    } for r in readers]


def _checksum(readers, edges):
    payload = {
        'schema': SCHEMA,
        'readers': sorted(_source_rows(readers), key=lambda r: r['code']),
        'relations': sorted(edges, key=lambda e: (e['a'], e['b'], e['basis'])),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _total_articles(rows):
    """篇数相加；任一份档案缺篇数时总篇数未知，返回 None，不按 0 处理。"""
    if any(r.get('weekly_reading') is None for r in rows):
        return None
    return round(sum(float(r['weekly_reading']) for r in rows), 6)


def _weighted(rows, field, total):
    """按周阅读篇数加权；缺值或总篇数为 0 时返回 None，不补 0、不补均值。"""
    if total is None or any(r.get(field) is None for r in rows):
        return None
    if total == 0:
        return None
    return round(sum(float(r['weekly_reading']) * float(r[field]) for r in rows) / total, 6)


def _base_identity(reader):
    weekly = reader.get('weekly_reading')
    zero = weekly == 0
    return {
        'canonical_code': reader['code'],
        'source_codes': [reader['code']],
        'size': 1,
        'merge_basis': [],
        'weekly_reading': weekly,
        'completion': None if zero else reader.get('completion'),
        'night_share': None if zero else reader.get('night_share'),
        'zero_articles': zero,
        'quarantined': False,
        'quarantine_reason': None,
        'unknown_codes': [],
    }


def build_identity_result(readers, relations, batch_id=None, name=''):
    """由冻结的档案快照和同人关系生成一份完整身份版本。纯函数，重试不会累加。"""
    readers = _source_rows(readers)
    edges, duplicates = dedupe_relations(relations)
    by_code = {r['code']: r for r in readers}
    union = _UnionFind()
    for r in readers:
        union.add(r['code'])
    for edge in edges:
        union.add(edge['a'])
        union.add(edge['b'])
        union.union(edge['a'], edge['b'])

    groups = defaultdict(set)
    for code in union.parent:
        groups[union.find(code)].add(code)
    edges_by_group = defaultdict(list)
    for edge in edges:
        edges_by_group[union.find(edge['a'])].append(edge)
    order = sorted(groups, key=lambda root: min(groups[root]))
    identities, quarantined = [], []
    for root in order:
        # 合并编号固定取组内字典序最小者，关系输入顺序不影响结果。
        codes = sorted(groups[root])
        basis = sorted(edges_by_group[root], key=lambda e: (e['a'], e['b'], e['basis']))
        item = {
            'canonical_code': codes[0],
            'source_codes': codes,
            'size': len(codes),
            'merge_basis': [{'a': e['a'], 'b': e['b'], 'basis': e['basis']} for e in basis],
        }
        unknown = [c for c in codes if c not in by_code]
        if unknown:
            item.update({
                'weekly_reading': None,
                'completion': None,
                'night_share': None,
                'zero_articles': False,
                'quarantined': True,
                'quarantine_reason': '同人关系组包含档案中不存在的编号：' + '、'.join(unknown),
                'unknown_codes': unknown,
            })
            quarantined.append(item)
            continue
        rows = [by_code[c] for c in codes]
        weekly_total = _total_articles(rows)
        if len(rows) == 1:
            # 单份档案：比例就是原始记录值，不需要加权，也不属于伪造。
            completion = rows[0].get('completion')
            night_share = rows[0].get('night_share')
        else:
            completion = _weighted(rows, 'completion', weekly_total)
            night_share = _weighted(rows, 'night_share', weekly_total)
        item.update({
            'weekly_reading': weekly_total,
            'completion': completion if weekly_total else None,
            'night_share': night_share if weekly_total else None,
            # 总篇数为 0 时比例没有定义：保留原始档案，但身份层面不补 0、不补均值。
            'zero_articles': weekly_total == 0,
            'quarantined': False,
            'quarantine_reason': None,
            'unknown_codes': [],
        })
        identities.append(item)

    identities.sort(key=lambda x: x['canonical_code'])
    quarantined.sort(key=lambda x: x['canonical_code'])
    stats = {
        'reader_codes': len(readers),
        'identities': len(identities),
        'merged_identities': sum(1 for x in identities if x['size'] > 1),
        'quarantined_groups': len(quarantined),
        'quarantined_codes': sum(x['size'] for x in quarantined),
        'zero_article_identities': sum(1 for x in identities if x['zero_articles']),
        'relations': len(edges),
        'duplicate_edges': duplicates,
    }
    return {
        'schema': SCHEMA,
        'batch_id': batch_id,
        'version': batch_id if batch_id is not None else 0,
        'name': name,
        'identities': identities,
        'quarantined': quarantined,
        'stats': stats,
        'checksum': _checksum(readers, edges),
        'method': '同人关系按并查集做传递合并；原始档案不改写；周阅读篇数相加；完读率与夜读占比按篇数加权；总篇数为0时比例保持未定义；含未知编号的连通组整组隔离。',
    }


def base_identity_result(readers, name='基础身份（未归并）'):
    """没有已完成归并批次时的身份版本：每个原始编号各算一人。

    单份档案无需加权，比例沿用原始记录，不属于伪造；只有多份合并且总篇数为0时才无定义。
    """
    identities = [_base_identity(r) for r in sorted(_source_rows(readers), key=lambda r: r['code'])]
    stats = {
        'reader_codes': len(identities),
        'identities': len(identities),
        'merged_identities': 0,
        'quarantined_groups': 0,
        'quarantined_codes': 0,
        'zero_article_identities': sum(1 for x in identities if x['zero_articles']),
        'relations': 0,
        'duplicate_edges': 0,
    }
    payload = [{'code': r['code'], 'weekly_reading': r['weekly_reading'], 'completion': r['completion'], 'night_share': r['night_share']} for r in sorted(_source_rows(readers), key=lambda r: r['code'])]
    return {
        'schema': SCHEMA,
        'batch_id': None,
        'version': 0,
        'name': name,
        'identities': identities,
        'quarantined': [],
        'stats': stats,
        'checksum': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'method': '尚未启用归并批次：每个原始编号视为一个身份。',
    }


def reader_rows(session):
    from src.models.entities import Reader
    return [{
        'id': r.id,
        'code': r.code,
        'weekly_reading': r.weekly_reading,
        'completion': r.completion,
        'night_share': r.night_share,
        'note': r.note or '',
    } for r in session.query(Reader).order_by(Reader.id).all()]


def load_identity_version(session):
    """读取一个完整、已生效的身份版本；批次结果不可变。

    只认最近一个 done 批次；queued/running/failed 批次不会部分生效。
    没有 done 批次时回退为基础身份（每个编号一人）。

    批次快照之后新建的档案不属于任何同人关系，以单编号身份追加到当前版本，
    避免新会员在一个已完成批次之后被分群静默漏掉；追加是确定性的，
    版本 checksum 由批次 checksum 与追加档案共同决定。
    档案修订不回写批次快照，已冻结的合并结果保持不变。
    """
    from src.models.entities import IdentityBatch
    batch = session.query(IdentityBatch).filter(IdentityBatch.state == 'done').order_by(IdentityBatch.id.desc()).first()
    if batch and isinstance(batch.result, dict) and 'identities' in batch.result:
        result = dict(batch.result)
        covered = set()
        for item in list(result.get('identities') or []) + list(result.get('quarantined') or []):
            covered.update(item.get('source_codes') or [])
        appended = [_base_identity(r) for r in sorted(reader_rows(session), key=lambda r: r['code']) if r['code'] not in covered]
        if appended:
            appended.sort(key=lambda x: x['canonical_code'])
            result['identities'] = sorted(list(result.get('identities') or []) + appended, key=lambda x: x['canonical_code'])
            stats = dict(result.get('stats') or {})
            stats['reader_codes'] = stats.get('reader_codes', 0) + len(appended)
            stats['identities'] = stats.get('identities', 0) + len(appended)
            stats['zero_article_identities'] = stats.get('zero_article_identities', 0) + sum(1 for x in appended if x['zero_articles'])
            result['stats'] = stats
            result['appended_codes'] = [x['canonical_code'] for x in appended]
            payload = {'batch_checksum': batch.checksum or result.get('checksum', ''), 'appended': [{'code': x['canonical_code'], 'weekly_reading': x['weekly_reading'], 'completion': x['completion'], 'night_share': x['night_share']} for x in appended]}
            result['checksum'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            result['batch_checksum'] = batch.checksum
        else:
            result['appended_codes'] = []
        return {
            'batch_id': batch.id,
            'version': batch.id,
            'name': batch.name,
            'state': batch.state,
            'result': result,
        }
    readers = reader_rows(session)
    return {
        'batch_id': None,
        'version': 0,
        'name': '基础身份（未归并）',
        'state': 'done',
        'result': base_identity_result(readers),
    }


def identity_cluster_rows(version):
    """把身份版本转成聚类输入：每个非隔离身份恰好一行。"""
    rows = []
    for index, item in enumerate(version['result'].get('identities', [])):
        if item.get('quarantined'):
            continue
        rows.append({
            'id': index + 1,
            'code': item['canonical_code'],
            'identity_key': item['canonical_code'],
            'source_codes': list(item['source_codes']),
            'size': item['size'],
            'merge_basis': item.get('merge_basis') or [],
            'zero_articles': bool(item.get('zero_articles')),
            **{feature: item.get(feature) for feature in FEATURES},
        })
    return rows


def _identity_index(version):
    index = {}
    for item in list(version['result'].get('identities', [])) + list(version['result'].get('quarantined', [])):
        for code in item.get('source_codes', []):
            index[code] = item
    return index


def resolve_outreach(segment, cluster_id, version):
    """用当前完整身份版本解析一个分群版本的成员。

    - 同一当前身份只保留一份，完成“按当前身份去重”；
    - 同一身份出现在多个群组时列为 cross_cluster 冲突；
    - 隔离身份、版本里找不到的编号也列为冲突，均阻止签核。
    """
    result = segment.result or {}
    raw_members = result.get('members') or []
    by_code = _identity_index(version)
    identity_clusters = defaultdict(set)
    identity_ref = {}
    raw_codes = 0

    for member in raw_members:
        cluster = member.get('cluster_id')
        codes = member.get('source_codes') or ([member['code']] if member.get('code') else [])
        for code in codes:
            raw_codes += 1
            item = by_code.get(code)
            if not item:
                item = {
                    'canonical_code': code,
                    'source_codes': [code],
                    'size': 1,
                    'quarantined': True,
                    'unresolved': True,
                    'quarantine_reason': '编号不在当前身份版本中',
                    'unknown_codes': [code],
                    'weekly_reading': None,
                    'completion': None,
                    'night_share': None,
                }
            key = item['canonical_code']
            identity_ref[key] = item
            identity_clusters[key].add(cluster)

    selected = sorted(key for key, clusters in identity_clusters.items() if cluster_id in clusters)
    conflicts, details = [], []
    for key in selected:
        item = identity_ref[key]
        clusters = sorted(identity_clusters[key])
        detail = {
            'canonical_code': key,
            'source_codes': list(item.get('source_codes') or [key]),
            'size': item.get('size', 1),
            'weekly_reading': item.get('weekly_reading'),
            'completion': item.get('completion'),
            'night_share': item.get('night_share'),
            'quarantined': bool(item.get('quarantined')),
            'clusters': clusters,
        }
        details.append(detail)
        if len(clusters) > 1:
            conflicts.append({
                'kind': 'cross_cluster',
                'identity': key,
                'source_codes': detail['source_codes'],
                'clusters': clusters,
                'reason': f"同人身份 {key} 出现在多个群组：{'、'.join('群' + str(c + 1) for c in clusters)}",
            })
        if item.get('quarantined'):
            conflicts.append({
                'kind': 'unresolved' if item.get('unresolved') else 'quarantined',
                'identity': key,
                'source_codes': detail['source_codes'],
                'clusters': clusters,
                'reason': f"身份 {key} 已隔离：{item.get('quarantine_reason') or '含未知编号'}",
            })

    return {
        'members': [d['canonical_code'] for d in details],
        'member_details': details,
        'conflicts': conflicts,
        'raw_codes': raw_codes,
        'unique_identities': len(selected),
    }
