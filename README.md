# 施工安全巡检证据链采集

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。`--capture` 只记录原始观察，不作业务投影；`--run` 工作流在同一状态库上做可复核的项目/隐患关联、附件证据归档与整改状态归并，支持多进程重启与全量幂等重算。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q003-safety-inspection:initial .
docker run --rm q003-safety-inspection:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_03 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_03.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /inspection
python3 -m collector_03.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /inspection
```

第一次 decoded=2、inserted=2，重抓同一观测 inserted=0。临时库不提交。tests 是已实现采集基础能力的回归测试，不代表扩展功能完成。

## 来源协议

XML record 的联合身份为 project、issue；不同项目可复用 issue。revision 在同一联合身份内递增。inspection 建立隐患，review 的 result=pass 且原检查与复查 attachment 均已采集时才能 closed；重大隐患其余情况为 stop，普通未销号为 open。withdraw 指向复查版本，撤销其销号效力而不删除历史。复查先到要暂存，检查到达后重新关联。

附件通过 /attachment/文件名 单独采集，以内容摘要归档，原 XML 记录保留来源 route。fetch_attachments=false 模拟附件暂未到达，必须进入 pending 并保留停止状态，不可用空附件充当证据。重复采集不增加历史，文件名相同内容变化保留两个版本；补采附件后允许重新计算但不伪造原取得时间。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 扩展接口与可观察输出

`python3 -m collector_03.cli --run 场景文件 --database 状态库 --output 结果文件`

场景文件含 name、run_id、sources（有序根来源）、options、fixture_root；name 只作日志标签。expected 是公开最小预期，产品不应读取它。所有来源和选项应独立支持；四个场景不能替代全部边界测试。

### 归档与关联规则

- 记录指纹为 `source(route) + 规范化 payload` 的 sha256，不含采集时间；同一记录跨进程重抓不会新增历史，重复导入只被跳过，不覆盖先归档版本。
- 同一 `(project, issue)` 出现相同 revision 但内容不同：两个版本都保留在 history，后到者进入 pending（`revision_conflict`），先归档版本保持权威。
- 联合身份尚无 inspection 时到达的 review/withdraw 标记 `linked=false` 并进入 pending（`awaiting_inspection`）；inspection 到达后的下一次运行自动重新关联并销号评估。
- 畸形记录（缺身份、未知 kind、revision 非正整数、withdraw 缺 target_revision）原样归档并进入 pending（`malformed_record`），绝不静默丢弃；severity 无法识别时按 major 失败关闭。
- withdraw 的 target_revision 未指向已归档 review 时进入 pending（`withdraw_target_missing`）；正常撤回只撤销该复查版本的销号效力，复查记录、附件与来源 route 全部保留。
- 附件以 `(filename, sha256)` 内容摘要归档，空响应体不成为证据且不落附件表；同名同内容重复采集不新增版本，同名不同内容保留两个版本并各自记录 archived_at，补采时间永不回填为原取得时间。附件名只允许纯文件名，路径形态进入 `invalid_attachment_ref`。
- `fetch_attachments=false` 时不发起附件请求，缺件记录进入 pending（`missing_attachment`），重大隐患保持 stop、普通隐患保持 open，禁止用空附件把重大隐患转为可复工；补采后下一次运行允许重新计算，pending 事件写 resolved_at 留痕。

### 业务投影

输出 JSON 包含：

- `issues`：按 project、issue 排序，含 state（closed/stop/open）与 severity；closed 仅在 pass 复查、检查与复查附件均已归档、且该复查未被 withdraw 撤销时成立。
- `history`：全部已归档 XML 记录的有序修订链，含 revision、kind、来源 source(route)、指纹、linked/canonical 标记、原始 payload、received_at；withdraw 条目顶层带 target_revision。
- `pending`：未结案的待核实队列，含 reason、project/issue/revision、detail、created_at；事件结案后保留 resolved_at 并不再出现在该数组。
- `attachments`：附件版本清单，含 filename、sha256、size_bytes、archived_at 与 linked_revisions（引用该附件的记录版本与来源）。
- `notifications`：持久化待处理通知清单（与未结案 pending 同构），`notifications_new` 为本次新建数量，重复运行应为 0。

数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。内部 SQLite 表、架构和调度实现由开发者选择。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影应保持一致，notifications 为数量时表示此次新建通知数，第二次应为0，为数组时表示持久化待处理通知清单。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。第二条运行四个公开场景（每个独立临时目录、执行两次、每次进程超时5秒），并在其后追加先复查后检查、附件后到两个合成回归；八次公开场景加回归的总预算40秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于90秒；依赖和镜像预先准备，不能用旧日志代替运行。请另加参数变化及边界回归，不修改预期来迁就实现。tests/test_workflow.py 覆盖领域规则与持久化边界（跨项目编号重用、同版本冲突、悬空撤回、畸形记录、空附件、同名改版）。
