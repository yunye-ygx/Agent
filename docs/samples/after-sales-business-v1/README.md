# 售后业务合成练习数据 v1

本目录全部为**合成练习数据**，不代表真实客户、真实订单或真实厂商参数。品牌「澄印」、NX100/NX200、故障码和售后规则均来自同仓库的教学语料。

- 基准日期：`2026-10-26`（Asia/Shanghai）
- 生成原因：让订单、设备、工单、保修状态和知识资料之间保持可追溯的闭合引用。
- 密码：不在任何 JSON 中。`seed_business` 交互输入密码，或通过 `--password-stdin` 从标准输入读取。

## 文件

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 版本、基准日期、计数、样例文件哈希，不含密码 |
| `users.json` | 1 个管理员、3 个客户，仅账号与显示信息 |
| `orders.json` | 12 笔订单及 12 条明细，覆盖 recorded/completed |
| `devices.json` | 18 台设备，购买日期与保修天数相对基准日生成 |
| `tickets.json` | 12 张工单及 35 条处理记录，覆盖四种状态 |
| `knowledge-manifest.json` | 6 份公开资料、7 条型号映射及源文件哈希 |

## 账号

| 用户名 | 角色 | 数据范围 |
| --- | --- | --- |
| `admin` | 管理员 | 全部客户、订单、设备、工单和资料开放 |
| `alice` | 客户 | 订单 1～4，设备 1～4，工单 1～4 |
| `bob` | 客户 | 订单 5～8，设备 5～8，工单 5～7、12 |
| `carol` | 客户 | 订单 9～12，设备 9～18，工单 8～11 |

各账号密码在初始化时自行设置，长度 12～128；示例和文档不记录任何实际密码。

## 覆盖场景

- 订单：`recorded` 8 笔、`completed` 4 笔；无 `cancelled` 样例，避免与设备引用冲突。
- 设备：NX100/NX200 混合；保修内 10 台、过保 8 台；1 台已退役；固件覆盖 1.2.0/1.4.2/1.5.0/1.6.x/2.0.x/2.1.0。
- 工单：`open` 3 张、`in_progress` 3 张、`resolved` 3 张、`closed` 3 张；跨越 3 个客户，包含退回、解决、关闭和追加备注。
- 知识：只开放 `corpus/01`～`06`；`07` 旧校准文档、`08`～`10` 历史工单和 `eval/` 评测题明确排除。

## 初始化

```powershell
# 只校验文件，不连接数据库
python -X utf8 -m app.cli.seed_business --dry-run --as-of 2026-10-26

# 显式导入；密码逐账号交互输入
python -X utf8 -m app.cli.seed_business --apply --yes --create-users

# 或由本地密钥工具按账号顺序通过标准输入提供密码
Get-Content .\local-secrets.txt | python -X utf8 -m app.cli.seed_business --apply --yes --create-users --password-stdin
```

重复执行只会跳过已存在的固定 ID / 自然键记录，不重复插入、不覆盖后续修改。自然键相同但 ID 不同的既有数据会停止导入并报告。

设备购买日期按 `purchased_days_before_as_of` 和 `warranty_days` 从基准日期计算；保修截止日包含当天。资料导入见：

```powershell
python -X utf8 -m app.cli.import_support_knowledge --dry-run
python -X utf8 -m app.cli.import_support_knowledge --status
```

只有已完整发布 generation 的文档才会进入客户检索范围。
