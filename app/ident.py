"""自适应初态辨识核心求解器。

模型（Mealy 机）：每个“状态—命令”对唯一确定 (下一状态, ASCII 响应)。

关键设计：

* 信念状态 = 若干“候选初态 -> 执行已发命令后当前所处状态”的关联。
  发出命令 c 后，按 *响应* 对信念分组：只有拿到相同回执的候选初态才留在同一
  分支里；分支内每个候选初态的当前位置随其各自的转移更新。这样始终保留
  “候选初态与命令后当前位置”的关联，绝不跨响应合并状态。
* 在信念状态图上做可达性博弈的吸引子分层（AND-OR 图）：
  深度 0 = 只剩一个候选初态；深度 d+1 = 存在命令使每个响应子信念的深度 ≤ d。
  首轮吸引子即最短最坏步数；吸引子永远到不了的信念为不可辨信念
  （含两个候选已走到同一当前状态的“重新混淆”，以及只在等深度信念间循环的情形）。
* 平局裁决全部使用 ASCII（字节）序：命令按 ASCII 升序、响应分支按 ASCII 升序，
  因此多个最短策略并存时得到唯一的规范树。
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

# 10 个候选时可达信念最多约 2^10 个，策略渲染沿深度链递归，放宽默认上限。
sys.setrecursionlimit(20_000)

# 信念节点数上限：状态 ≤10、命令 ≤6 时正常用例远不会触及，仅作保护。
BELIEF_LIMIT = 100_000
_CANCEL_EVERY = 512


class SpecError(ValueError):
    """录入数据不合法。"""


class Cancelled(Exception):
    """求解被外部取消。"""


class AnalysisLimit(Exception):
    """信念空间超过预算。"""


# 允许的可打印 ASCII（含空格）。
def _ascii_bytes(value: str) -> bytes:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SpecError(f"必须为 ASCII：{value!r}") from exc
    return raw


def _check_printable(name: str, value: str) -> str:
    raw = _ascii_bytes(value)
    if not value:
        raise SpecError(f"{name}不能为空")
    if any(b < 0x20 or b > 0x7E for b in raw):
        raise SpecError(f"{name}只能包含可打印 ASCII 字符：{value!r}")
    return value


@dataclass(frozen=True)
class Spec:
    states: tuple[str, ...]
    candidates: tuple[str, ...]
    commands: tuple[str, ...]
    # (状态, 命令) -> (下一状态, 响应)
    transitions: dict[tuple[str, str], tuple[str, str]]

    def step(self, state: str, command: str) -> tuple[str, str]:
        return self.transitions[(state, command)]


def _unique(name: str, values: list[str]) -> list[str]:
    seen: set[str] = set()
    for v in values:
        if v in seen:
            raise SpecError(f"{name}存在重复项：{v!r}")
        seen.add(v)
    return values


def build_spec(payload: dict) -> Spec:
    """从前端 JSON 构造并严格校验规格。"""
    states_raw = payload.get("states")
    commands_raw = payload.get("commands")
    candidates_raw = payload.get("candidates")
    table_raw = payload.get("transitions")

    if not isinstance(states_raw, list) or not (2 <= len(states_raw) <= 10):
        raise SpecError("状态数量必须在 2 到 10 之间")
    if not isinstance(commands_raw, list) or not (1 <= len(commands_raw) <= 6):
        raise SpecError("命令数量必须在 1 到 6 之间")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise SpecError("候选初态至少选择一个")

    states = _unique("状态", [_check_printable("状态名", str(s)) for s in states_raw])
    commands = _unique("命令", [_check_printable("命令", str(c)) for c in commands_raw])
    state_set = set(states)
    candidates = [str(c) for c in candidates_raw]
    if len(set(candidates)) != len(candidates):
        raise SpecError("候选初态存在重复")
    for c in candidates:
        if c not in state_set:
            raise SpecError(f"候选初态必须是已定义状态：{c!r}")

    if not isinstance(table_raw, dict):
        raise SpecError("转移表缺失")

    transitions: dict[tuple[str, str], tuple[str, str]] = {}
    for s in states:
        row = table_raw.get(s)
        if not isinstance(row, dict):
            raise SpecError(f"状态 {s!r} 的转移行缺失")
        for c in commands:
            cell = row.get(c)
            if not isinstance(cell, dict):
                raise SpecError(f"转移项缺失：状态={s!r} 命令={c!r}")
            nxt = cell.get("next")
            resp = cell.get("response")
            if not isinstance(nxt, str) or nxt not in state_set:
                raise SpecError(
                    f"下一状态必须是已定义状态：状态={s!r} 命令={c!r}"
                )
            if not isinstance(resp, str):
                raise SpecError(f"响应缺失：状态={s!r} 命令={c!r}")
            _check_printable("响应", resp)
            transitions[(s, c)] = (nxt, resp)

    # 规范化排序，保证输出与裁决稳定。
    return Spec(
        states=tuple(sorted(states, key=lambda x: x.encode("ascii"))),
        candidates=tuple(
            sorted(set(candidates), key=lambda x: x.encode("ascii"))
        ),
        commands=tuple(sorted(commands, key=lambda x: x.encode("ascii"))),
        transitions=transitions,
    )


# 信念键：((候选初态, 当前状态), ...)，按候选初态 ASCII 序排列。
Belief = tuple[tuple[str, str], ...]


def _split(spec: Spec, belief: Belief, command: str) -> list[tuple[str, Belief]]:
    """按响应分支：返回 [(响应, 子信念)]，响应按 ASCII 序。"""
    groups: dict[str, list[tuple[str, str]]] = {}
    for initial, current in belief:
        nxt, response = spec.step(current, command)
        groups.setdefault(response, []).append((initial, nxt))
    ordered = sorted(groups.items(), key=lambda kv: kv[0].encode("ascii"))
    return [(resp, tuple(sorted(pairs, key=lambda p: p[0].encode("ascii"))))
            for resp, pairs in ordered]


def _merged(belief: Belief) -> bool:
    """两个候选初态已处于同一当前位置——此后任何命令都无法再分开。"""
    currents = [cur for _, cur in belief]
    return len(set(currents)) < len(currents)


@dataclass
class _Graph:
    # 信念发现序号（BFS：命令 ASCII 序、响应 ASCII 序），用于稳定引用。
    order: list[Belief]
    index: dict[Belief, int]
    # belief -> 命令 -> [(响应, 子信念)]
    edges: dict[Belief, dict[str, list[tuple[str, Belief]]]]


def _enumerate(spec: Spec, is_cancelled: Callable[[], bool]) -> _Graph:
    if is_cancelled():
        raise Cancelled()
    root = tuple((c, c) for c in spec.candidates)
    index = {root: 0}
    order = [root]
    edges: dict[Belief, dict[str, list[tuple[str, Belief]]]] = {}
    queue = deque([root])
    ticks = 0

    while queue:
        belief = queue.popleft()
        cmd_map: dict[str, list[tuple[str, Belief]]] = {}
        for cmd in spec.commands:
            branches = _split(spec, belief, cmd)
            resolved_branches: list[tuple[str, Belief]] = []
            for response, child in branches:
                # 信念的身份就是完整的“候选初态 -> 当前位置”关联：位置集合相同
                # 但归属不同（如 {A→X,B→Y} 与 {A→Y,B→X}）是不同信念，绝不能仅按
                # 位置集合合并，否则共同回执分支会把初态与位置、后续回执对错位。
                if child not in index:
                    if len(index) >= BELIEF_LIMIT:
                        raise AnalysisLimit(
                            f"可达信念状态超过 {BELIEF_LIMIT} 个，模型过大"
                        )
                    index[child] = len(order)
                    order.append(child)
                    queue.append(child)
                resolved_branches.append((response, child))
            cmd_map[cmd] = resolved_branches
        edges[belief] = cmd_map
        ticks += 1
        if ticks % _CANCEL_EVERY == 0 and is_cancelled():
            raise Cancelled()

    return _Graph(order=order, index=index, edges=edges)


def analyze(
    spec: Spec,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """求解，返回可 JSON 序列化的结果。"""
    is_cancelled = is_cancelled or (lambda: False)
    graph = _enumerate(spec, is_cancelled)

    # ---- 吸引子分层：depth[b] = 从信念 b 出发的最短最坏步数 ----
    depth: dict[Belief, int] = {}
    dq: deque[Belief] = deque()
    for b in graph.order:
        if len(b) == 1:
            depth[b] = 0
            dq.append(b)

    # 每个 (信念, 命令) 还在等待多少个响应分支取得深度；
    # 以及到“最深子信念”的当前最大深度。
    remaining: dict[tuple[Belief, str], int] = {}
    deepest: dict[tuple[Belief, str], int] = {}
    predecessors: dict[Belief, list[tuple[Belief, str]]] = {}
    for b in graph.order:
        if len(b) == 1:
            continue
        for cmd, branches in graph.edges[b].items():
            key = (b, cmd)
            remaining[key] = len(branches)
            deepest[key] = 0
            for _, child in branches:
                predecessors.setdefault(child, []).append((b, cmd))

    tick = 0
    while dq:
        child = dq.popleft()
        d = depth[child]
        for parent, cmd in predecessors.get(child, []):
            key = (parent, cmd)
            if key not in remaining:
                continue
            if d > deepest[key]:
                deepest[key] = d
            remaining[key] -= 1
            if remaining[key] == 0:
                del remaining[key]
                if parent not in depth:
                    depth[parent] = deepest[key] + 1
                    dq.append(parent)
        tick += 1
        if tick % _CANCEL_EVERY == 0 and is_cancelled():
            raise Cancelled()

    winning = set(depth)
    root = graph.order[0]
    root_won = root in winning

    # 规范策略：最短最坏深度，平局取命令 ASCII 序。
    # 即使根信念不可辨，某些响应子信念仍可能可解（兄弟分支不可解才令父信念失败），
    # 因此要对所有 winning 信念计算策略。
    chosen: dict[Belief, str] = {}
    for b in graph.order:
        if len(b) == 1 or b not in depth:
            continue
        best: Optional[tuple[int, str]] = None
        for cmd in spec.commands:
            children = [child for _, child in graph.edges[b][cmd]]
            if all(c in depth for c in children):
                val = 1 + max(depth[c] for c in children)
                if val != depth[b]:
                    continue
                if best is None or cmd.encode("ascii") < best[1].encode("ascii"):
                    best = (val, cmd)
        if best is not None:
            chosen[b] = best[1]

    # ---- 信念索引（供页面引用 / 循环标记）----
    belief_index = []
    for bid, b in enumerate(graph.order):
        belief_index.append(
            {
                "id": bid,
                "pairs": [[i, cur] for i, cur in b],
                "terminal": len(b) == 1,
                "winning": b in winning,
                "merged": _merged(b),
                "depth": depth.get(b),
            }
        )

    ambiguous_ids = [
        graph.index[b] for b in graph.order if b not in winning and len(b) > 1
    ]

    inlined: set[int] = set()

    def belief_ref(b: Belief) -> dict:
        return {"belief": graph.index[b],
                "pairs": [[i, cur] for i, cur in b]}

    def render(b: Belief) -> dict:
        bid = graph.index[b]
        if len(b) == 1:
            return {"type": "resolved", **belief_ref(b),
                    "initial": b[0][0]}
        if b in winning:
            cmd = chosen[b]
            return {
                "type": "command",
                **belief_ref(b),
                "command": cmd,
                "depth": depth[b],
                "branches": [
                    {
                        "response": resp,
                        **belief_ref(child),
                        "node": render(child),
                    }
                    for resp, child in graph.edges[b][cmd]
                ],
            }
        # 不可辨信念：展示全部命令分支；同一信念只内联一次，其余以引用呈现。
        if bid in inlined:
            return {"type": "ambiguous_ref", **belief_ref(b)}
        inlined.add(bid)
        return {
            "type": "ambiguous",
            **belief_ref(b),
            "reason": "merged" if _merged(b) else "unresolvable",
            "branches": [
                {
                    "command": cmd,
                    "responses": [
                        {"response": resp, **belief_ref(child),
                         "node": render(child)}
                        for resp, child in graph.edges[b][cmd]
                    ],
                }
                for cmd in spec.commands
            ],
        }

    tree = render(root)

    return {
        "status": "distinguishable" if root_won else "indistinguishable",
        "worst_case_depth": depth.get(root),
        "states": list(spec.states),
        "candidates": list(spec.candidates),
        "commands": list(spec.commands),
        "beliefs": belief_index,
        "ambiguous_beliefs": ambiguous_ids,
        "tree": tree,
    }


def analyze_payload(
    payload: dict,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    return analyze(build_spec(payload), is_cancelled)
