# 山岚 · 内容订阅行为分群

将会员阅读档案转为可解释的行为群组，再制定带冻结成员清单的站内触达方案。

## 真实业务场景

模拟客户山岚内容社有会员运营组4人、编辑6人，试点240位订阅会员。原流程是运营人员导出 `山岚_会员周阅读_20261109.xlsx`，按阅读次数手工分组，再把名单和选题建议发给编辑。三个痛点是篇数与比例量纲混用、分群名单随工作簿修订漂移、内容建议无法追溯到当时的行为画像。本项目承接上游最近完整自然周的行为聚合，替代手工分组与名单归档，下游由编辑和运营负责人审阅内容主题、渠道假设及频次。

业务模块：阅读档案（录入/完整字段修订）、身份归并（同人多编号的传递合并、整组隔离）、特征选择（2–3维、k=2–6）、分群图谱（标准化散点、轮廓系数、原始单位画像、群成员）、触达方案（按当前身份去重、冻结名单、站内起草、冲突阻止、负责人签核与导出）。三类核心实体为Reader、IdentityBatch（另含Segment、Outreach）；另有用户与审计记录。不连接真实会员系统或外发服务。

|角色|权限和页面|典型动作|
|---|---|---|
|admin|全部页面、审计和独占签核|核对内容主题和频次后签核|
|analyst|档案读写、归并批次、分群任务、起草与导出|提交同人关系，选择行为维度，形成群组内容草案|
|viewer|业务只读与导出，后端拒绝写操作|核对身份版本、画像和冻结成员|

## 一天的工作剧本

1. 08:50 viewer查看SL-001至SL-240的周阅读篇数、完读率、夜读占比。
2. 09:00 analyst核对输入来自同一完整自然周，修订说明保留在note。
3. 09:10 新增 `SL-241`，周阅读15篇、完读率0.8、夜读占比0.2。
4. 09:15 analyst发现同一会员在三个渠道留下SL-003/SL-083/SL-163，在身份归并提交链式关系；批次完成后三份档案合并为一份身份。含未知编号的组整组隔离。
5. 09:20 在特征选择创建“11月会员阅读分群”，勾选全部3个维度、k=3；聚类输入是当前身份版本，每个身份一份合并档案。
6. 09:30 worker执行标准化KMeans，点击刷新查看轮廓系数和原始单位画像。
7. 09:40 点击群组切换成员表，核对人数与阅读习惯，不把群号当作价值排名。
8. 10:00 起草“低频读者轻阅读试点”，绑定分群版本和群1，说明每周一次短篇精选；草案按当前身份去重，跨群同人列为冲突。
9. 10:10 admin签核（存在冲突时后端拒绝），10:20 viewer下载 `reader-outreach-<id>.json`，获得固定成员清单。

## 身份归并口径

同一会员用多个编号订阅时，逐编号分群会把一人算成多人。身份归并批次接收同人关系（`A,B,依据`），用并查集做传递闭包：A-B、B-C 会把 A/B/C 合并为一份身份，合并编号取组内字典序最小的来源编号。

- **原始档案只读保留**：`readers` 行不被改写、合并或删除；归并结果只存在于批次版本中，`GET /readers` 始终返回全部原始编号。
- **篇数相加**：合并身份的周阅读篇数为来源编号之和。
- **比例按篇数加权**：完读率、夜读占比以周阅读篇数加权；总篇数为 0 时保持 `null`（页面显示“未定义”），不补 0、不补均值。
- **未知编号整组隔离**：关系里出现档案中不存在的编号时，该连通组整体进入 `quarantined`，不参与分群；补录档案后新建批次即可解除。
- **重复边只算一次**：A-B、B-A、重复提交视为同一条边，批次统计 `duplicate_edges`；自环（A-A）在 422 拒绝。
- **批次整体生效**：批次经 Redis 队列由 worker 处理，只有 `done` 才成为当前身份版本；`queued/running/failed` 不会部分覆盖。结果由创建时冻结的档案快照与关系一次性重建，中断重试不重复累加（worker 在数据库行锁内把 `queued` 改为 `running`，再次投递时非 `queued` 直接跳过）。
- **批次后新增编号**：已完成批次快照之后录入的编号不参与任何同人关系，以单编号身份追加到当前版本（`appended_codes`），避免新会员被静默漏掉；追加是确定性的，档案修订不回写批次快照。
- **各自读取同一完整身份版本**：归并批次写入、档案修订、分群创建、触达起草/签核各自在同一事务/同一读取中取用最近一个 `done` 批次；分群创建时把 `identity_batch_id/identity_version/identity_checksum` 冻结进版本，之后的新批次不会改写已冻结分群。

## 触达与冲突

- 分群成员表按身份列出：一行一份合并档案，同时展示来源编号、编号数与合并依据。
- 触达草案按当前身份版本**去重**：同一人的多个编号只保留一份合并档案，`raw_codes` 记录去重前编号数。
- **跨群同人**（同一身份出现在多个群组）、隔离身份、身份版本漂移（草案名单与当前版本不一致）都列为冲突，`approve` 返回 409 并阻止签核；冲突明细写入草案并在页面展示。
- **已签核原名单保留**：签核时把名单与当时的身份版本、checksum、时间写入 `approval_*` 字段；之后新增归并批次不会改变已签核方案的 `members`，导出即冻结清单。

## 计算口径和失败态

输入为会员唯一编号和最近完整自然周的3个聚合指标：周阅读篇数0–1000、完读率0–1、夜读占比0–1。夜读口径22:00–06:00由上游聚合保证，本应用不重新采集阅读事件。选择2–3个不同特征，k限定2–6。聚类输入是当前身份版本中的每份非隔离合并档案（每身份一行），不是原始编号行。

Python先用StandardScaler的总体均值和总体标准差生成z=(x−mean)/std，再运行KMeans欧氏距离，`n_init=20, random_state=3107, algorithm=lloyd`。所有身份共同用于无监督聚类，不宣称预测评估。轮廓系数s=(b−a)/max(a,b)，a为同群平均距离，b为最近其他群平均距离，范围−1至1；较高表示当前标准化空间中分离更清晰，不表示业务价值。

中心用scaler.inverse_transform还原为原始单位；画像另统计群内三个原始维度的均值（即使某维未选入聚类也展示其描述均值；该维在身份层面未定义时跳过并显示“未定义”）。群号按逆变换中心排序，仅保证同一输入和seed可复核，不跨版本等价。散点图展示前两个所选标准化维度，聚类仍用所有选择维度，二维重叠不代表同群。

样本数≤k、所选常量列、唯一特征点少于k、实际群数不足、缺失/非有限值均明确失败；不静默补值、降k或删除样本。所选特征在任一身份上为“未定义”（例如零篇数合并身份的比例）时，同样 422 失败并提示改选特征或修订档案。输入验证422，重复会员编号409，队列故障503。任务失败保存failed和错误原因，修订后新建版本。创建时冻结输入与身份版本，worker保存成员、中心、画像、轮廓系数、scaler参数、seed和checksum。触达草案保存版本成员副本，不随后续档案修改漂移。分群只描述行为相似性，不判断会员价值或因果关系。

## 领域事件并发

- 档案修订只改 `readers`，不回头改写已冻结的归并批次快照或分群版本。
- 归并批次结果由 `snapshot` + `relations` 纯函数重建；worker 完成时把 `result/checksum/state` 一次事务提交，重复投递时状态已非 `queued` 直接跳过，因此中断重试不会重复累加篇数。
- 分群与触达在各自请求内只调用一次 `load_identity_version`：读到哪一版就整份使用，不会一半旧版一半新版。
- 触达签核另外要求草案名单与当前身份版本解析结果一致；不一致时写回 `identity_drift` 冲突并 409，而不是静默改名单。已签核方案不再重算。


## 快速启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 [localhost:18206](http://localhost:18206)，后端 [localhost:19206/docs](http://localhost:19206/docs)，PostgreSQL映射17706。演示账号 `admin@demo.local`、`analyst@demo.local`、`viewer@demo.local`，密码均 `Demo123!`。seed=3107，240会员；自然键只插入缺失账号和档案，重复执行不覆盖人工修改。`SEED_DEMO=true`时启动自动执行一次，失败记录日志但不阻塞服务。

五服务frontend/backend/worker/db/redis均有健康检查。backend等待数据库/Redis，worker还等待backend，frontend等待backend。worker真实消费Redis的 `reader:identities` 与 `reader:segments` 两个队列，心跳 `/tmp/heartbeat`；单worker启动恢复queued/running。数据库、Redis和产物用命名卷，产物位于共享 `/artifacts`（`identity-<id>.json`、`<segment-id>.json`）。Nginx将 `/api/`转发到后端根路由。显式子网10.253.107.0/24规避当前机器默认地址池耗尽，可通过NETWORK_SUBNET调整。结束用 `docker compose down -v --remove-orphans` 清理本项目数据。

## 技术、目录与本地开发

|层|实现|
|---|---|
|前端|原生HTML/CSS/ES modules，Nginx1.27|
|后端|Python3.11、FastAPI、SQLAlchemy、PyJWT|
|计算|scikit-learn1.6、NumPy2.2、SciPy1.14|
|数据与队列|PostgreSQL16、Redis7|

`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}` 分离网络、状态、契约、共用表格/字段/状态标签、表单工具、页面和枚举。`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}` 分离HTTP、查询、算法、持久化、校验、鉴权和配置。RequestId与SecurityHeaders为两个中间件，全局错误可恢复，业务写入有审计。ORM建初表，migrations/001_initial.sql与002_identity_merge.sql保留实际DDL，没有在线版本迁移执行器；已有库升级需手工执行002。

本地开发先启动db/redis，在backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET后运行 `uvicorn src.main:app --reload`。前端静态文件通过Nginx同源代理，也可配置本地开发代理。环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET。部署时替换演示凭据与密钥。

共享枚举ROLES、STATES、FEATURES、OUTREACH_STATES在后端src/constants/enums.py和前端src/constants/enums.js；字段约束在schemas/business.py，持久化列在models/entities.py。前端共用table/fields/status，表单hooks和HTML转义utils至少两个工具。

## 范围与证据

逐条表单/API录入及完整字段修订，同人关系逐条提交（无CSV/Excel导入、删除、分页、在线新会员预测或自动跨版本群组对齐）。触达仅站内方案与签核，不发送邮件或消息。单worker；不宣称多worker exactly-once或大规模性能。Google Fonts需联网，离线使用serif/sans-serif回退。已知差异、真实测试和截图位于源码外 `../../test-artifacts/gb-3107/`。许可证MIT，见LICENSE。
