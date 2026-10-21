# 自动评测动手实验手册

这份手册的目标不是让你背指标，而是让你亲自经历一次完整的评测闭环：

```text
提出假设 → 只改一个变量 → 运行评测 → 找到变化的层级 → 解释原因 → 修复 → 回归验证
```

所有实验都使用当前已经导入的 `docs/leave_policy.md`。每次运行前，建议把“失败重试”设为 `0`，这样结果更容易观察。

## 实验 1：Top-K 和 Rerank 到底改变了什么

### 问题

```text
连续休假超过3个工作日需要提前多久提交申请？
```

### 操作

在“自动化评测”面板中只选择：

```text
Top-K：1、3、5
Rerank：关闭、开启
```

运行后，在 `leave-002` 的每个结果中记录：

| 配置 | candidate_pool_hit | retrieval_hit | answer_pass | citation_pass | case_pass |
|---|---|---|---|---|---|
| Top-K=1，关闭 |  |  |  |  |  |
| Top-K=1，开启 |  |  |  |  |  |
| Top-K=3，关闭 |  |  |  |  |  |
| Top-K=3，开启 |  |  |  |  |  |
| Top-K=5，关闭 |  |  |  |  |  |
| Top-K=5，开启 |  |  |  |  |  |

### 你要验证的假设

```text
Top-K 越大，正确 Chunk 越有机会进入最终上下文；
Rerank 可能把原始向量排名靠后的正确 Chunk 提到前面。
```

### 重点观察

- `candidate_pool_hit=false`：Embedding 候选池里根本没有满足 source/evidence 的 Chunk。
- `candidate_pool_hit=true` 但 `retrieval_hit=false`：候选池找到了，但最终 Top-K 或 Rerank 没选中。
- `retrieval_hit=true` 但 `answer_pass=false`：资料找对了，是 LLM 理解/表达问题。
- `answer_pass=true` 但 `citation_pass=false`：答案文字碰巧正确，但引用不可靠。

不要只看整场评测的总分，要先看同一个 case 在不同配置下的变化。

## 实验 2：故意制造一个“总分提高但重要问题退化”的场景

这个实验模拟真实项目中最容易误判的情况：只看平均分，会掩盖关键问题失败。

### 操作

1. 先运行完整配置矩阵，保存 `leave-002`（连续休假申请）和 `leave-003`（待审批额度）结果。
2. 把 `leave-002` 当作关键业务问题；它的通过要求是 `case_pass=true`。
3. 再运行一组你认为整体更快的配置，例如 `Top-K=1，Rerank=关闭`。
4. 对比所有 case 的平均结果，以及 `leave-002` 的单独结果。

### 你要回答

```text
即使平均延迟下降或其他 case 通过率上升，leave-002 失败时能不能接受？
失败发生在候选池、最终检索、答案还是引用？
```

### 结论标准

```text
总体指标是优化目标；关键 case 是质量底线。
关键 case 退化时，先定位并修复，不能被平均分掩盖。
```

## 实验 3：区分“模型答错”和“评测标准写错”

这个实验复现你刚刚遇到的 `leave-003` 问题。

### 正确的原文

```text
2.4 只有 OA 审批状态为“已通过”的申请，才计入已使用年假。
草稿、已撤回和被驳回的申请不扣减额度。
```

### 操作思路

先查看 `leave-003` 的标准 evidence，再查看召回 Chunk。检查每个 evidence 是否真的出现在原文中。

如果标准 evidence 写成了问题中的词（例如“待审批”），会出现：

```text
模型答案看起来正确
answer_pass=true
candidate_pool_hit=false
retrieval_hit=false
```

这时不要立刻改模型。先问：

```text
评测器要求的证据，是否真的存在于 source 文档？
```

把 evidence 改成原文实际存在的短语（例如 `2.4`、`已通过`、`不扣减额度`）后重新运行。

### 这个实验说明

```text
评测失败不一定代表系统失败，也可能是评测标准不准确。
标准答案和 evidence 本身也需要人工审查。
```

## 实验 4：把真实失败案例变成回归保护网

### 场景

你在问答页面输入：

```text
如果突然住院，来不及提前申请怎么办？
```

假设系统错误地回答成“提前5个工作日提交申请”。

### 人工确认的正确依据

```text
2.3 紧急就医或直系亲属突发事件无法提前申请的，
应在返岗后 2 个工作日内补交说明和证明材料。
```

### 操作

把这个问题整理成新的评测 case：

```json
{
  "case_key": "leave-006",
  "question": "如果突然住院，来不及提前申请怎么办？",
  "expected_sources": ["leave_policy.md"],
  "expected_evidence": ["2.3", "紧急就医", "返岗后 2 个工作日"],
  "expected_answer": "紧急就医无法提前申请时，应在返岗后2个工作日内补交说明和证明材料。",
  "required_keywords": ["紧急就医", "返岗后", "2个工作日"],
  "forbidden_keywords": ["提前5个工作日"]
}
```

之后每次修改 Chunk、Embedding、Rerank 或 Prompt，都必须重新跑它。这样这个曾经出现过的真实问题就不会被遗忘。

## 实验记录模板

每次实验只填写一张：

```text
实验名称：
问题：
基线配置：
本次只修改的变量：
预期变化：
实际变化：
candidate_pool_hit：
retrieval_hit：
answer_pass：
citation_pass：
case_pass：
失败发生在哪一层：
我的解释：
修复动作：
修复后结果：
```

先做实验 1，不要同时改 Chunk、Embedding 和 Prompt。第一轮只需要把 `leave-002` 的六组结果记录下来，就能看到一个完整的“配置变化 → 检索变化 → 答案变化”的因果链。
