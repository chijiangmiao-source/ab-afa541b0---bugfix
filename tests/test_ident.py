import unittest

from app.ident import (
    Cancelled,
    SpecError,
    analyze,
    analyze_payload,
    build_spec,
)


def make(states, commands, transitions, candidates=None):
    """transitions: {state: {cmd: (next, response)}}"""
    payload = {
        "states": states,
        "commands": commands,
        "candidates": candidates if candidates is not None else list(states),
        "transitions": {
            s: {c: {"next": n, "response": r} for c, (n, r) in row.items()}
            for s, row in transitions.items()
        },
    }
    return build_spec(payload)


def find(tree, node_type):
    out = []
    def walk(n):
        if n.get("type") == node_type:
            out.append(n)
        for b in n.get("branches", []):
            walk(b.get("node", {}))
            for r2 in b.get("responses", []):
                walk(r2.get("node", {}))
    walk(tree)
    return out


class TestOneDepthTree(unittest.TestCase):
    """两个初态在同一命令下得到不同响应 → 深度一呈现响应分支及对应初态。"""

    def test_depth_one(self):
        spec = make(
            ["S0", "S1"], ["a"],
            {"S0": {"a": ("S0", "X")}, "S1": {"a": ("S1", "Y")}},
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "distinguishable")
        self.assertEqual(res["worst_case_depth"], 1)
        root = res["tree"]
        self.assertEqual(root["type"], "command")
        self.assertEqual(root["command"], "a")
        self.assertEqual([b["response"] for b in root["branches"]], ["X", "Y"])
        leaves = [b["node"] for b in root["branches"]]
        self.assertEqual([n["initial"] for n in leaves], ["S0", "S1"])
        for n in leaves:
            self.assertEqual(n["type"], "resolved")

    def test_depth_one_with_three_candidates(self):
        spec = make(
            ["A", "B", "C"], ["c"],
            {s: {"c": (s, resp)} for s, resp in zip("ABC", ["1", "2", "3"])},
        )
        res = analyze(spec)
        self.assertEqual(res["worst_case_depth"], 1)
        # 响应按 ASCII 序，初态随之确定。
        self.assertEqual(
            [(b["response"], b["node"]["initial"]) for b in res["tree"]["branches"]],
            [("1", "A"), ("2", "B"), ("3", "C")],
        )

    def test_response_order_ascii(self):
        # 响应 "9" < "A" < "a"（字节序）。
        spec = make(
            ["s1", "s2", "s3"], ["x"],
            {"s1": {"x": ("s1", "a")}, "s2": {"x": ("s2", "A")},
             "s3": {"x": ("s3", "9")}},
        )
        res = analyze(spec)
        self.assertEqual(
            [b["response"] for b in res["tree"]["branches"]], ["9", "A", "a"]
        )
        self.assertEqual(
            [b["node"]["initial"] for b in res["tree"]["branches"]],
            ["s3", "s2", "s1"],
        )


class TestIndistinguishable(unittest.TestCase):
    def test_identical_rows(self):
        # 两个候选对每个命令都有相同响应且汇流 → 不可辨；一步后信念重新混淆。
        spec = make(
            ["S0", "S1"], ["a"],
            {"S0": {"a": ("S0", "R")}, "S1": {"a": ("S0", "R")}},
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "indistinguishable")
        self.assertIsNone(res["worst_case_depth"])
        amb = res["tree"]
        self.assertEqual(amb["type"], "ambiguous")
        # 根信念候选位置为 S0、S1（未合并）；命令 a 的唯一响应把它们带到同一位置。
        self.assertEqual(amb["reason"], "unresolvable")
        # 子信念 {S0→S0, S1→S0} 是“重新混淆”的不可再分信念，必须可达且展示。
        child_node = amb["branches"][0]["responses"][0]["node"]
        self.assertEqual(child_node["type"], "ambiguous")
        self.assertEqual(child_node["reason"], "merged")
        merged = [b for b in res["beliefs"] if b["merged"]]
        self.assertEqual(len(merged), 1)

    def test_split_then_remerge(self):
        """响应能暂时区分，但两个候选在一步后走到同一当前位置 → 重新混淆。"""
        # 命令 a：S0->S0 回 X；S1->S0 回 Y。信念按响应分为 {S0@S0} 与 {S1@S0}，
        # 第二支当前位置 S0 与初态 S0 关联仍保留；后续命令若再发 a，
        # 两个候选都在 S0 → 合并。这里候选集 {S0,S1}，一次 a 已经各成单点，
        # 所以构造更细：三候选，其中两个暂时分开后汇合。
        spec = make(
            ["P", "Q", "M"], ["g"],
            # P、Q 响应不同但都进入 M；M 自身响应 Z。
            {"P": {"g": ("M", "x")}, "Q": {"g": ("M", "y")},
             "M": {"g": ("M", "z")}},
            candidates=["P", "Q"],
        )
        res = analyze(spec)
        # 一步后两支都是单点初态（各分支内只剩一个候选），所以其实可辨识。
        self.assertEqual(res["status"], "distinguishable")
        self.assertEqual(res["worst_case_depth"], 1)

    def test_cycle_without_resolution(self):
        """响应暂时把信念切成两个多候选信念，二者互相可达却永不收敛。"""
        # 两个候选在命令 a 下响应相同（不分组），且在 S0/S1 间互换：
        # 信念永远含两个候选。
        spec = make(
            ["S0", "S1"], ["a"],
            {"S0": {"a": ("S1", "r")}, "S1": {"a": ("S0", "r")}},
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "indistinguishable")
        amb = res["tree"]
        self.assertEqual(amb["type"], "ambiguous")
        self.assertEqual(amb["reason"], "unresolvable")
        # 展示各命令分支。
        self.assertEqual(amb["branches"][0]["command"], "a")
        # 一步后信念 {S0→S1, S1→S0} 仍是多候选不可辨信念，首次完整展示；
        # 再一步又循环回去，之后以引用呈现（不内联爆炸）。
        child_node = amb["branches"][0]["responses"][0]["node"]
        self.assertEqual(child_node["type"], "ambiguous")
        self.assertNotEqual(child_node["belief"], amb["belief"])
        grand = child_node["branches"][0]["responses"][0]["node"]
        self.assertEqual(grand["type"], "ambiguous_ref")
        self.assertEqual(grand["belief"], amb["belief"])

    def test_command_branches_listed_on_ambiguous(self):
        spec = make(
            ["S0", "S1"], ["a", "b"],
            {"S0": {"a": ("S0", "r"), "b": ("S0", "r")},
             "S1": {"a": ("S1", "r"), "b": ("S1", "r")}},
        )
        res = analyze(spec)
        amb = res["tree"]
        self.assertEqual([bc["command"] for bc in amb["branches"]], ["a", "b"])

    def test_losing_root_has_winning_multicandidate_child(self):
        """根不可辨（一支汇流），但另一响应子信念 {Q,R} 仍可再分。

        这类子树是多候选可解信念，规范策略必须照样给出命令；
        回归：早期实现仅在根可辨时计算策略，渲染到该子树会 KeyError。
        """
        spec = make(
            ["P", "Q", "R", "S", "T", "M", "P0", "Q1", "R1"],
            ["a"],
            {
                "P": {"a": ("P0", "X")},
                "Q": {"a": ("Q1", "Y")},
                "R": {"a": ("R1", "Y")},
                "S": {"a": ("M", "Z")},
                "T": {"a": ("M", "Z")},
                "M": {"a": ("M", "z")},
                "P0": {"a": ("P0", "x0")},
                "Q1": {"a": ("Q1", "lo")},
                "R1": {"a": ("R1", "hi")},
            },
            candidates=["P", "Q", "R", "S", "T"],
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "indistinguishable")
        root = res["tree"]
        self.assertEqual(root["type"], "ambiguous")
        by_resp = {r["response"]: r for r in root["branches"][0]["responses"]}
        # X 支：单候选立即收敛。
        self.assertEqual(by_resp["X"]["node"]["type"], "resolved")
        # Z 支：两个候选汇流到同一位置 → 重新混淆。
        z_node = by_resp["Z"]["node"]
        self.assertEqual(z_node["type"], "ambiguous")
        self.assertEqual(z_node["reason"], "merged")
        # Y 支：{Q,R} 处于不同位置，再发 a 得 lo/hi → 必须是策略命令节点。
        y_node = by_resp["Y"]["node"]
        self.assertEqual(y_node["type"], "command")
        self.assertEqual(y_node["command"], "a")
        self.assertEqual(y_node["depth"], 1)
        self.assertEqual(
            sorted((b["response"], b["node"]["initial"])
                   for b in y_node["branches"]),
            [("hi", "R"), ("lo", "Q")],
        )


class TestAssociationKept(unittest.TestCase):
    """候选初态与命令后当前位置的关联必须保留：同响应后位置不同则继续可分。"""

    def test_same_response_different_positions_can_diverge_later(self):
        # 根处 a、b 都只能给同响应并把两个候选带到不同位置 (X, Q)；
        # 到达信念 {P→X, Q→Q} 后，再发 b，X 回 lo、Q 回 hi 才能分开。
        spec = make(
            ["P", "Q", "X"], ["a", "b"],
            {
                "P": {"a": ("X", "r"), "b": ("X", "s")},
                "Q": {"a": ("Q", "r"), "b": ("Q", "s")},
                "X": {"a": ("X", "r"), "b": ("X", "lo")},
            },
            candidates=["P", "Q"],
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "distinguishable")
        self.assertEqual(res["worst_case_depth"], 2)
        root = res["tree"]
        # 深度 1 命令 a 只有一个响应分支 r，含两个候选；深度 2 用 b 分开。
        self.assertEqual(root["command"], "a")
        self.assertEqual(len(root["branches"]), 1)
        b0 = root["branches"][0]
        self.assertEqual(b0["response"], "r")
        # 信念关联：P→X, Q→Q
        pairs = sorted(tuple(p) for p in b0["node"]["pairs"])
        self.assertEqual(pairs, [("P", "X"), ("Q", "Q")])
        inner = b0["node"]
        self.assertEqual(inner["command"], "b")
        self.assertEqual(
            sorted((x["response"], x["node"]["initial"]) for x in inner["branches"]),
            [("lo", "P"), ("s", "Q")],
        )


class TestDepthTwoOptimal(unittest.TestCase):
    def test_worst_case_two(self):
        # 命令 a：S0 回 0 且进入可一步分辨区；构造标准二步例子。
        # S0-a->S0/0；S1-a->S1/0（同响应，仍混淆）
        # 命令 b：S0->S0/hot，S1->S1/cold → b 可一步分辨。
        # 若 b 是最短 1 步，策略应选 b（深度1），所以让 b 在根不行：
        spec = make(
            ["S0", "S1", "T"], ["a", "b"],
            {
                "S0": {"a": ("S0", "0"), "b": ("T", "z")},
                "S1": {"a": ("S1", "1"), "b": ("T", "z")},
                "T": {"a": ("T", "t"), "b": ("T", "z")},
            },
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "distinguishable")
        self.assertEqual(res["worst_case_depth"], 1)
        self.assertEqual(res["tree"]["command"], "a")

    def test_genuine_depth_two(self):
        # 根：a 把 {P,Q,R} 分成 {P} 与 {Q,R}（Q→U, R→V，需再一步）。
        # b 在根不能一步分辨（Q、R 同为 two/same），到达 U/V 后才分开。
        spec = make(
            ["P", "Q", "R", "U", "V"], ["a", "b"],
            {
                "P": {"a": ("P", "one"), "b": ("P", "p")},
                "Q": {"a": ("U", "two"), "b": ("U", "same")},
                "R": {"a": ("V", "two"), "b": ("V", "same")},
                "U": {"a": ("U", "lo"), "b": ("U", "lo")},
                "V": {"a": ("V", "hi"), "b": ("V", "hi")},
            },
        )
        res = analyze(spec)
        self.assertEqual(res["status"], "distinguishable")
        self.assertEqual(res["worst_case_depth"], 2)
        root = res["tree"]
        self.assertEqual(root["command"], "a")
        # 分支 one 立即解析；two 再用 a（U/V 响应 lo/hi，平局 ASCII 序取 a）解析。
        by = {b["response"]: b for b in root["branches"]}
        self.assertEqual(by["one"]["node"]["type"], "resolved")
        self.assertEqual(by["two"]["node"]["command"], "a")
        self.assertEqual(
            sorted((x["response"], x["node"]["initial"])
                   for x in by["two"]["node"]["branches"]),
            [("hi", "R"), ("lo", "Q")],
        )


class TestStableTieBreaking(unittest.TestCase):
    def test_command_ascii_tiebreak(self):
        # 命令 b 和 a 都能一步分辨；规范树必须选 ASCII 较小的 a。
        spec = make(
            ["S0", "S1"], ["b", "a"],
            {
                "S0": {"a": ("S0", "X"), "b": ("S0", "X")},
                "S1": {"a": ("S1", "Y"), "b": ("S1", "Y")},
            },
        )
        res = analyze(spec)
        self.assertEqual(res["tree"]["command"], "a")
        # 输出命令列表也按 ASCII 排序。
        self.assertEqual(res["commands"], ["a", "b"])

    def test_deterministic_across_input_permutations(self):
        def run(cmd_order):
            spec = make(
                ["S0", "S1"], cmd_order,
                {
                    "S0": {"a": ("S0", "X"), "b": ("S0", "P"), "c": ("S0", "M")},
                    "S1": {"a": ("S1", "Y"), "b": ("S1", "Q"), "c": ("S1", "N")},
                },
            )
            tree = analyze(spec)["tree"]
            return tree["command"], [b["response"] for b in tree["branches"]]

        self.assertEqual(run(["c", "b", "a"]), run(["a", "b", "c"]))
        self.assertEqual(run(["c", "b", "a"])[0], "a")


class TestValidation(unittest.TestCase):
    def base(self):
        return {
            "states": ["S0", "S1"],
            "commands": ["a"],
            "candidates": ["S0", "S1"],
            "transitions": {
                "S0": {"a": {"next": "S0", "response": "X"}},
                "S1": {"a": {"next": "S1", "response": "Y"}},
            },
        }

    def test_state_count_bounds(self):
        p = self.base()
        p["states"] = ["S0"]
        with self.assertRaises(SpecError):
            analyze_payload(p)
        p["states"] = [f"S{i}" for i in range(11)]
        p["transitions"] = {s: {"a": {"next": s, "response": "r"}}
                            for s in p["states"]}
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_command_count_bounds(self):
        p = self.base()
        p["commands"] = []
        with self.assertRaises(SpecError):
            analyze_payload(p)
        p["commands"] = [f"c{i}" for i in range(7)]
        p["transitions"] = {
            s: {c: {"next": s, "response": "r"} for c in p["commands"]}
            for s in p["states"]
        }
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_non_ascii_rejected(self):
        p = self.base()
        p["transitions"]["S0"]["a"]["response"] = "中文"
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_missing_transition(self):
        p = self.base()
        del p["transitions"]["S1"]["a"]
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_bad_candidate(self):
        p = self.base()
        p["candidates"] = ["S0", "ZZ"]
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_empty_candidates(self):
        p = self.base()
        p["candidates"] = []
        with self.assertRaises(SpecError):
            analyze_payload(p)

    def test_single_candidate_resolves_zero_steps(self):
        p = self.base()
        p["candidates"] = ["S1"]
        res = analyze_payload(p)
        self.assertEqual(res["worst_case_depth"], 0)
        self.assertEqual(res["tree"]["type"], "resolved")
        self.assertEqual(res["tree"]["initial"], "S1")


class TestCancellation(unittest.TestCase):
    def test_pre_cancelled(self):
        spec = make(
            ["S0", "S1"], ["a"],
            {"S0": {"a": ("S0", "X")}, "S1": {"a": ("S1", "Y")}},
        )
        with self.assertRaises(Cancelled):
            analyze(spec, lambda: True)


if __name__ == "__main__":
    unittest.main()
