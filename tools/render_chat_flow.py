"""Render the chat request flow to a PNG (matplotlib, no graphviz needed)."""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon

OUT = "chat-flow.png"

for _name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC"):
    try:
        font_manager.findfont(_name, fallback_to_default=False)
        FAMILY = _name
        break
    except Exception:
        continue
else:
    FAMILY = "DejaVu Sans"

matplotlib.rcParams["font.family"] = FAMILY
matplotlib.rcParams["axes.unicode_minus"] = False

STEP = ("#e8f0fe", "#2563eb", "#1e3a8a")
ERR = ("#fee2e2", "#dc2626", "#991b1b")
OK = ("#dcfce7", "#16a34a", "#14532d")
DEC = ("#fef3c7", "#d97706", "#78350f")
IO = ("#1f2937", "#0f172a", "#ffffff")
PANEL = ("#f8fafc", "#94a3b8", "#334155")
ROW = ("#ffffff", "#cbd5e1", "#334155")
GREY = "#475569"

fig, ax = plt.subplots(figsize=(14.2, 13.9), dpi=160)
ax.set_xlim(0, 14.2)
ax.set_ylim(0, 13.9)
ax.set_aspect("equal")
ax.axis("off")


def box(cx, cy, w, h, text, style=STEP, fs=9.2, ls="-", lw=1.5, z=3, linespacing=1.55):
    fc, ec, tc = style
    ax.add_patch(
        FancyBboxPatch(
            (cx - w / 2, cy - h / 2),
            w,
            h,
            boxstyle="round,pad=0,rounding_size=0.10",
            facecolor=fc,
            edgecolor=ec,
            linewidth=lw,
            linestyle=ls,
            zorder=z,
        )
    )
    ax.text(
        cx, cy, text, ha="center", va="center",
        fontsize=fs, color=tc, zorder=z + 1, linespacing=linespacing,
    )
    return (cx, cy, w, h)


def diamond(cx, cy, w, h, text, fs=9.5):
    fc, ec, tc = DEC
    ax.add_patch(
        Polygon(
            [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)],
            closed=True, facecolor=fc, edgecolor=ec, linewidth=1.5, zorder=3,
        )
    )
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color=tc, zorder=4)
    return (cx, cy, w, h)


def arrow(p1, p2, color=GREY, lw=1.6, ls="-", label=None, label_dx=0.14, ms=15):
    ax.add_patch(
        FancyArrowPatch(
            p1, p2, arrowstyle="-|>", mutation_scale=ms, linewidth=lw,
            color=color, linestyle=ls, shrinkA=0, shrinkB=0, zorder=2,
        )
    )
    if label:
        ax.text(
            (p1[0] + p2[0]) / 2 + label_dx,
            (p1[1] + p2[1]) / 2,
            label, ha="left", va="center", fontsize=8.6, color=color, zorder=6,
            bbox=dict(facecolor="white", edgecolor="none", pad=1.2),
        )


def down(a, b, label=None):
    arrow((a[0], a[1] - a[3] / 2), (b[0], b[1] + b[3] / 2), label=label)


def right(a, b, label="失败"):
    color = "#dc2626"
    ax.add_patch(
        FancyArrowPatch(
            (a[0] + a[2] / 2, a[1]), (b[0] - b[2] / 2, b[1]),
            arrowstyle="-|>", mutation_scale=14, linewidth=1.4, color=color,
            linestyle=(0, (4, 2)), shrinkA=0, shrinkB=0, zorder=2,
        )
    )
    ax.text(
        (a[0] + a[2] / 2 + b[0] - b[2] / 2) / 2, a[1] + 0.15, label,
        ha="center", va="bottom", fontsize=8.2, color=color, zorder=6,
        bbox=dict(facecolor="white", edgecolor="none", pad=1.0),
    )


SX, SX_W = 4.55, 5.9
EX, EX_W = 10.85, 4.5

n_start = box(
    SX, 13.15, SX_W + 0.6, 0.52,
    'POST /api/v1/chat/conversations/{id}/messages      {"content": "..."}', IO, fs=9.4,
)
n1 = box(
    SX, 12.10, SX_W, 0.86,
    "① 前置校验（FastAPI 层，未进业务代码）\n"
    "SendMessage：严格类型 · 非空 · ≤8000 字符 · 多余字段拒绝\n"
    "Origin 同源 · Session → Principal · CSRF 比对 · 取 AsyncSession",
)
e1 = box(EX, 12.10, EX_W, 0.62, "422 / 401 / 403\n数据库零写入", ERR, fs=8.6)

n2 = box(SX, 11.02, SX_W, 0.58, "② 归属校验\nget_conversation(me, cid)")
e2 = box(EX, 11.02, EX_W, 0.62, "404\n不存在 / 不是我的", ERR, fs=8.6)

n3 = box(
    SX, 10.00, SX_W, 0.80,
    "③ 原子抢轮（CAS）\n"
    "turn_id = uuid()\n"
    "UPDATE active_turn = turn_id  WHERE active_turn IS NULL",
)
d1 = diamond(SX, 9.02, 2.1, 0.74, "抢到？")
e3 = box(EX, 9.02, EX_W, 0.62, "409 CHAT_BUSY\n已有轮在跑", ERR, fs=8.6)

n4 = box(
    SX, 7.95, SX_W, 0.98,
    "④ 写用户消息（与 ③ 同一事务）\n"
    "SELECT … FOR UPDATE：验票 · 锁行 · 取 next_sequence\n"
    "INSERT seq=next_sequence · status=pending · next_sequence+1\n"
    "首轮（next_sequence==2）设标题 → COMMIT",
)
e4 = box(EX, 7.95, EX_W, 0.62, "409 CHAT_TURN_CLOSED\n票已作废", ERR, fs=8.6)

n5 = box(
    SX, 6.68, SX_W, 0.92,
    "⑤ prepare\n"
    "默认模型必须恰好 1 条（0 条或 >1 条都 503）\n"
    "history：仅 status=complete 的整轮，≤20 轮 / 6 万字符\n"
    "build_agent：只读 + 当前用户有权限的工具",
)
e5 = box(
    EX, 6.68, EX_W, 0.72,
    "503 CHAT_MODEL_MISSING\nfinish_turn(failed) 释放锁", ERR, fs=8.6,
)

n6 = box(
    SX, 5.70, SX_W, 0.66,
    "⑥ db.rollback() 释放连接 → 返回 StreamingResponse\n"
    "HTTP 200 已定 · 之后只能发 error 事件 · yield start",
)

# ---------------- 循环容器 ----------------
ax.add_patch(
    FancyBboxPatch(
        (1.55, 2.85), 6.05, 2.30,
        boxstyle="round,pad=0,rounding_size=0.12",
        facecolor=PANEL[0], edgecolor=PANEL[1], linewidth=1.3,
        linestyle=(0, (5, 3)), zorder=1,
    )
)
ax.text(
    1.75, 4.95,
    "⑦ ReAct 循环  agent.astream（recursion_limit = 24）",
    ha="left", va="center", fontsize=9.0, color=PANEL[2],
    fontweight="bold", zorder=5,
)

rows = [
    ("模型调用：body = system + 历史 + 工具 schema（每步重新拼）", 0.36, 8.4),
    ("delta（逐字）→ 直接推前端，不落库", 0.36, 8.4),
    ("message（assistant / tool）→ 校验 → 落库 → 推前端", 0.36, 8.4),
    ("tool_calls → ToolRuntime.execute\n结构→存在→权限→参数→authorize→超时执行 → 落库 tool 消息", 0.56, 8.2),
]
row_y = [4.55, 4.13, 3.71, 3.19]
for (text, rh, rfs), y in zip(rows, row_y):
    box(4.90, y, 5.0, rh, text, ROW, fs=rfs, z=4, linespacing=1.35)

# 左侧回环箭头：最后一行 → 第一行
lx = 1.95
ax.plot([2.40, lx, lx], [3.19, 3.19, 4.55], color="#64748b", lw=1.2, zorder=2)
ax.add_patch(
    FancyArrowPatch(
        (lx, 4.55), (2.40, 4.55), arrowstyle="-|>", mutation_scale=13,
        linewidth=1.2, color="#64748b", shrinkA=0, shrinkB=0, zorder=2,
    )
)
ax.text(
    1.76, 3.87, "工具结果回填\n回到模型继续",
    ha="center", va="center", rotation=90, fontsize=7.8, color="#475569", zorder=6,
)

d2 = diamond(SX, 2.26, 2.1, 0.74, "完整？")
e6 = box(
    EX, 2.26, EX_W, 0.82,
    "error 事件 CHAT_FAILED\nfinally：finish_turn(failed)\n（已写消息保留，整轮不进历史）",
    ERR, fs=8.4,
)

n7 = box(
    SX, 1.26, SX_W, 0.84,
    "⑧ finish_turn(succeeded=True)\n"
    "active_turn=NULL · 整轮 status=complete\n"
    "yield done → 关流",
    OK,
)

down(n_start, n1)
down(n1, n2)
down(n2, n3)
down(n3, d1)
down(d1, n4, "是")
down(n4, n5)
down(n5, n6)
arrow((SX, 5.37), (SX, 5.15))
arrow((SX, 2.85), (SX, 2.63))
down(d2, n7, "是")

right(n1, e1)
right(n2, e2)
right(d1, e3, "否")
right(n4, e4, "票失效")
right(n5, e5, "失败")
right(d2, e6, "否")

# 图例
ly = 0.45
items = [(IO, "入口"), (STEP, "正常步骤"), (DEC, "决策"), (ERR, "失败出口"), (OK, "成功结束")]
x = 2.4
for style, label in items:
    ax.add_patch(
        FancyBboxPatch(
            (x, ly - 0.13), 0.32, 0.26,
            boxstyle="round,pad=0,rounding_size=0.06",
            facecolor=style[0], edgecolor=style[1], linewidth=1.2,
        )
    )
    ax.text(x + 0.44, ly, label, ha="left", va="center", fontsize=8.6, color="#334155")
    x += 1.55

_dir = os.path.dirname(OUT)
if _dir:
    os.makedirs(_dir, exist_ok=True)
fig.savefig(OUT, bbox_inches="tight", facecolor="white")
print("font:", FAMILY)
print("saved:", os.path.abspath(OUT))