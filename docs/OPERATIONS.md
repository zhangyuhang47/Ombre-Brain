# Ombre Brain 可靠性与恢复手册

这份文档说明 Ombre Brain 在断网、模型限流、外部编辑和备份恢复时真正保证什么。

## 数据边界

- `buckets/**/*.md` 是记忆真源。写入成功以 Markdown 原子落盘为准。
- `embeddings.db`、BM25 缓存和脱水缓存都是可重建的派生数据。
- `.embedding_outbox.json` 只保存待索引 ID、内容哈希和重试状态，不复制记忆正文。
- `config.yaml`、`.env`、API Key、OAuth/Tunnel token 不进入本地记忆导出包。

## 写入与恢复保证

1. embedding 不可用、限流或超时时，Markdown 仍先保存，后台 outbox 持久重试。
2. 连续 provider 故障会打开全局熔断，避免每条待办都重复撞击同一个故障端点；冷却后自动恢复，也可在 Dashboard 手动补齐。
3. Obsidian、Git 或手工修改 Markdown 后，BucketManager 会按配置的轮询间隔发现文件集合/mtime/size 变化，刷新内存与 BM25，并只对正文变化重新排队向量。
4. 本地导出对正在使用的 SQLite 调用 backup API，得到事务一致快照；不会直接复制可能处于 WAL 写入中的数据库文件。
5. 新导出包含 `backup_manifest.json`，逐文件记录字节数与 SHA-256。恢复预检要求清单与 ZIP 内容完全一致。

清单只能发现残缺或意外篡改，不能证明备份由谁创建。需要来源认证时，应在可信存储或带签名的发布/备份系统中保管 ZIP。

## 日常检查

Dashboard 的“系统诊断”与命令行使用同一套只读检查：

```bash
python tools/check_buckets.py
python tools/check_buckets.py --json
```

检查项包括：

- Markdown 是否都能以 UTF-8 + frontmatter 解析；
- 是否存在重复 bucket ID 或指向 vault 外的软链接；
- `embeddings.db` 的 `PRAGMA quick_check`；
- 已没有对应 Markdown 的孤儿向量；
- 活跃 Markdown 缺向量时，是否已经进入 outbox。

## 备份与恢复演练

1. 在 Dashboard 导出完整记忆包，确认请求成功且文件非空。
2. 准备一个全新的临时 vault/测试实例，不要直接覆盖唯一的生产目录。
3. 在迁移页面上传 ZIP。新包应显示“备份清单与 SHA-256 校验通过”；旧包会显示“未验证”。
4. 检查 bucket 数、冲突决策和 embedding 模型/维度，再执行导入。
5. 导入完成后运行 `python tools/check_buckets.py`，并用 `breath(query=...)` 抽查可检索性。
6. 确认 outbox 待处理数最终回到 0。模型离线时允许保持 pending，但 Markdown 必须完整可读。

导入冲突的语义：

- `skip`：保留当前记忆，不导入冲突项。
- `keep_both`：导入项获得新 ID；可复用的向量同步映射到新 ID。
- `overwrite`：当前项不会被物理抹去，而是归档并获得唯一的 `*-superseded-*` 历史 ID；导入项接管原 ID。

## 故障处置

| 现象 | 数据状态 | 处理 |
|---|---|---|
| embedding 超时/限流 | Markdown 已保存，向量 pending | 检查网络/额度；等待熔断冷却或手动补齐 |
| 语义检索不可用 | 关键词/BM25 仍可读，返回明确降级提示 | 修复 provider 后等待 outbox 清空 |
| Obsidian 修改后结果旧 | 等待外部变更轮询周期 | 检查 `storage.external_change_poll_seconds`，再看系统诊断的外部变更计数 |
| ZIP 上传被拒绝 | 本地 vault 未写入 | 按错误修复损坏、路径穿越、重复项或清单不一致，重新导出 |
| SQLite quick_check 失败 | Markdown 真源通常仍在 | 先备份 Markdown，移走损坏的派生库，再重建向量；不要删除 Markdown |
| outbox 长时间不下降 | 记忆正文仍安全 | 查看熔断状态、最近错误、Key/模型/维度和 provider 连通性 |

## 配置

```yaml
storage:
  external_change_poll_seconds: 1.0

embedding:
  background_indexing: true
  retry_base_seconds: 5
  retry_max_seconds: 300
  circuit_failure_threshold: 3
  circuit_base_seconds: 30
  circuit_max_seconds: 600
```

轮询设为 `0` 表示每次活跃桶列表读取都检查文件状态。生产环境一般保留 `1.0`，避免高频目录扫描。
