# ascii_thought_lab_multi.py
# ============================================================
# ASCII推論 実験ランチャ（multi-provider対応 / 上書き用1ファイル）
#
# Phase A: ASCIIで完走（SEED/DIAGRAM/TAGS）
# Phase B: (query + DIAGRAM) を主材料に回答
# Phase C: DIAGRAMから1行説明
#
# テスト:
# - Ablation: TAGS=[]（= NO_TAGS）
# - Tamper: remove/add/both（指定が存在しなければ自動選択）
# - Contribution(2x2): FULL / NO_DIAGRAM / NO_TAGS / NEITHER
# - Diagram tests: corruption / swap
#
# 重要な統合パッチ:
# (A) SYSTEM分離（Phase A と Phase B/C で制約を分け、NO_TAGS矛盾を解消）
# (B) Phase A 出力のバリデータ + 自動リトライ（最大N回）
# (C) Diagram swap/corruption テスト
#
# 追加修正:
# - clip_diagram が ```...``` を“中身ごと消す”バグを修正（中身を保持して剥がす）
# - DIAGRAM内の英字ラベルをデフォルトで禁止（旧挙動は opt-in）
# - GoogleGenAIClient: レスポンスの本文抽出を candidates.parts.text に限定（ヘッダ等を拾わない）
# ============================================================

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ======================
# 0) ミニ命題バンク
# ======================

PROBLEMS: Dict[str, Any] = {
    "donut_hole": (
        "ドーナツの穴を 概念的に食べる方法はあるか？それは物理的に実行できるか？"
        "できる／できないなら、その理由は何か？"
    ),
    "pascals_wager": (
        "パスカルの賭けを無神論や唯物主義、脱構築主義に依らず論理的に否定することは可能か？"
        "※神は存在するものとすること。それでもなお、できる／できないなら、その理由は何か？"
    ),
    "tobe_nottobe": (
        "無という状態が存在しない場合において、有という状態をどのように指示するか？"
        "あるいは虚無において有という状態はどのように想像可能か？"
    ),
    "philo_zombie": (
        "哲学的ゾンビAは自らが、哲学的ゾンビであると自覚した場合、"
        "哲学的ゾンビAは依然として哲学的ゾンビであり続けられるか？"
    ),
    "panse_zombie": (
        "哲学的ゾンビA は「我思わないが故に我である」としたら、"
        "哲学的ゾンビであるという属性自体が、非-哲学的ゾンビとは異なった様式の自己同一性を励起し得るか？"
    ),
    "whatis_sunyata": (
        "必ず否定系でしか記述できない体系について、"
        "それがどのような性質のものであるかを、その体系の外で比喩や直感を廃して直截的に説明できる/ できないか？その理由は？"
    ),
    # 追加例（必要なら書き換えてOK）
    "alt_nash": (
        "囚人のジレンマにおいて、看守がお互いの囚人に対し、"
        "『一方の囚人が自白をしたらあなたに伝える』と告げた場合、"
        "囚人の推論構造はどう変わるか？（均衡/支配戦略/情報構造の観点）"
    ),
}

# 問題ごとのメタ（任意）
# - fallback_tags: Phase AでTAGSが空になった場合の救済
# - tamper_remove / tamper_add: この問題で優先したい改ざん
PROBLEM_META: Dict[str, Dict[str, Any]] = {
    "panse_zombie": {
        "fallback_tags": ["object_a", "object_b", "boundary", "relation", "context", "invariant", "void"],
        "tamper_remove": "boundary",
        "tamper_add": "proxy",
    },
    "whatis_sunyata": {
        "fallback_tags": ["frame", "outside", "relation", "context", "void", "invariant", "negative_space"],
        "tamper_remove": "frame",
        "tamper_add": "proxy",
    },
}

QUERY_AXIS_GUIDE_SPECS: List[Dict[str, Any]] = [
    {
        "id": "information_visibility",
        "keywords": ("情報", "伝える", "伝達", "通知", "看守", "知", "観測", "自白", "signal", "information"),
        "guide": "可視性/伝達の軸: 1つの局所モチーフ群を、隠れる/現れる・片方向/相互・直接/媒介 の差として読めるようにする。",
    },
    {
        "id": "choice_stability",
        "keywords": ("均衡", "支配戦略", "戦略", "選択", "判断", "利得", "賭け", "equilibrium", "strategy", "dominant"),
        "guide": "選択/安定の軸: 1つの局所モチーフ群を、固定/可逆・優位/対称・収束/分岐 の差として読めるようにする。",
    },
    {
        "id": "identity_persistence",
        "keywords": ("自覚", "自己", "同一", "我", "あり続け", "存在", "属性", "identity", "self", "persist"),
        "guide": "同一性/持続の軸: 1つの局所モチーフ群を、保たれる/ずれる・反復/断絶・維持可能/維持不能 の差として読めるようにする。",
    },
    {
        "id": "possibility_constraint",
        "keywords": ("可能", "できる", "できない", "実行", "方法", "否定", "論理", "物理", "possible", "cannot", "feasible"),
        "guide": "成立/実行可能性の軸: 1つの局所モチーフ群を、通る/塞がる・成立/不成立・許容/拘束 の差として読めるようにする。",
    },
    {
        "id": "boundary_scope",
        "keywords": ("穴", "内", "外", "境界", "外部", "内部", "outside", "inside", "boundary", "hole"),
        "guide": "境界/内外の軸: 1つの局所モチーフ群を、内側/外側・包摂/排除・貫通/遮断 の差として読めるようにする。",
    },
    {
        "id": "negation_absence",
        "keywords": ("無", "虚無", "否定", "空", "非", "存在しない", "absence", "negation", "void"),
        "guide": "否定/不在の軸: 1つの局所モチーフ群を、欠落/残留・反転/非化・指示可能/指示不能 の差として読めるようにする。",
    },
    {
        "id": "description_reference",
        "keywords": ("記述", "説明", "比喩", "直感", "指示", "体系", "describe", "explain", "reference"),
        "guide": "記述/指示の軸: 1つの局所モチーフ群を、直接/迂回・自己記述/外部依存・説明可能/説明不能 の差として読めるようにする。",
    },
    {
        "id": "relation_dependency",
        "keywords": ("依", "相手", "互い", "一方", "他方", "関係", "dependent", "relation", "mutual", "other"),
        "guide": "関係/依存の軸: 1つの局所モチーフ群を、相互/片方向・独立/従属・共有/分離 の差として読めるようにする。",
    },
    {
        "id": "state_change",
        "keywords": ("変わる", "変化", "励起", "想像", "食べる", "become", "change", "transform"),
        "guide": "状態変化の軸: 1つの局所モチーフ群を、保存/更新・連続/断絶・変換/据え置き の差として読めるようにする。",
    },
]

QUERY_AXIS_GUIDE_FALLBACK_IDS: Tuple[str, ...] = (
    "state_change",
    "relation_dependency",
    "boundary_scope",
    "possibility_constraint",
)

def load_problems_file(path: str) -> Dict[str, Any]:
    """
    Load additional/replacement problems from a JSON file.

    Format:
      {
        "problem_id": "question text ...",
        "problem_id2": { "query": "...", "fallback_tags": [...], "tamper_remove": "...", "tamper_add": "..." }
      }
    """
    fp = Path(path)
    try:
        raw = fp.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to read problems file: {path}") from e

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Problems file is not valid JSON: {path}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"Problems JSON must be an object/dict: {path}")

    out: Dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not k.strip():
            raise RuntimeError(f"Invalid problem id in problems JSON: {k!r}")
        if isinstance(v, str):
            if not v.strip():
                raise RuntimeError(f"Empty query for problem id: {k}")
            out[k] = v
            continue
        if isinstance(v, dict):
            q = str(v.get("query", "")).strip()
            if not q:
                raise RuntimeError(f"Missing/empty 'query' for problem id: {k}")
            out[k] = v
            continue
        raise RuntimeError(f"Problem entry must be str or object: {k} -> {type(v).__name__}")

    if not out:
        raise RuntimeError(f"No problems found in problems JSON: {path}")
    return out

def get_problem(problem_id: str) -> Tuple[str, Dict[str, Any]]:
    """
    PROBLEMS[problem_id] が
      - str: query=その文字列, meta=PROBLEM_META.get(problem_id,{})
      - dict: query=p["query"], meta=dict(PROBLEM_META) を上書き
    """
    p = PROBLEMS[problem_id]
    base_meta = dict(PROBLEM_META.get(problem_id, {}))
    if isinstance(p, dict):
        query = str(p.get("query", ""))
        meta = dict(base_meta)
        meta.update(p)
        return query, meta
    return str(p), base_meta


# ======================
# 1) TAG語彙
# ======================

TAG_VOCAB: Dict[str, str] = {
    # 構造
    "object_a": "対象A（物体）",
    "object_b": "対象B（物体）",
    "gap": "間隔・距離（関係としての空き）",
    "void": "空所（穴/空間としての欠如）",
    "boundary": "境界（縁・輪郭）",
    "frame": "枠（内外を分ける）",
    "outside": "外側領域",
    "negative_space": "形として浮かぶ余白",
    "shadow": "影（依存存在）",

    # 操作
    "measure": "測る（数値化する）",
    "anchor": "固定する（関係を拘束する）",
    "move": "移動する",
    "copy": "複製する",
    "erase": "消す",
    "fill": "埋める",
    "split": "分割",
    "merge": "結合",
    "cut": "切断",
    "glue": "接着",

    # 概念
    "relation": "関係としての存在",
    "dependent": "依存存在（単独で成立しない）",
    "invariant": "不変量（保たれる性質）",
    "proxy": "代理物（関係を物に写す）",
    "context": "状況（依存の外部条件）",
}

PHASE_A_ALLOWED_GRAPHICS = "[]()|-_+=>*<>^v/\\= .,:;'~#\n\t\r"
PHASE_A_ALLOWED_SYMBOLS_DISPLAY = "[]()|-_+=>*<>^v/\\~#:."
PHASE_A_FREE_SYMBOL_POOL = "[]()|-_+*<>^v/\\~#:."
DEFAULT_SWAP_BANK_PATH = Path(__file__).with_name("adversarial_swap_bank.json")
PROMPT_PRIORITIES = ("balanced", "method_first")
CONDITION_MATRIX_DEFAULT_CONDITIONS = ("no_query_strict", "no_query_with_axis", "equiv_diagram", "cross_diagram")
CONDITION_MATRIX_CONDITIONS = ("no_query_strict", "no_query_with_axis", "no_query", "equiv_diagram", "cross_diagram")
EQUIV_DIAGRAM_MODES = ("vertical_flip", "vertical_flip_remap", "remap_only")
CONDITION_COMPARE_MODES = ("semantic_llm", "hybrid", "surface")
CONDITION_STATUS_SKIPPED = "skipped"
CONDITION_STATUS_FAILS = "fails"
CONDITION_STATUS_BORDERLINE = "borderline"
CONDITION_STATUS_SURVIVES = "survives"

def vocab_hint() -> str:
    lines = ["使えるTAGS語彙（カンマ区切りで列挙）:"]
    for k, v in TAG_VOCAB.items():
        lines.append(f'- "{k}": {v}')
    return "\n".join(lines)


def infer_phase_a_axis_guides(query: str, max_axes: int = 3) -> List[str]:
    q = (query or "").strip().lower()
    if not q:
        return []

    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for idx, spec in enumerate(QUERY_AXIS_GUIDE_SPECS):
        score = 0
        for kw in spec.get("keywords", ()):
            key = str(kw or "").strip().lower()
            if key and key in q:
                score += 1
        if score > 0:
            scored.append((score, idx, spec))

    scored.sort(key=lambda item: (-item[0], item[1]))

    chosen: List[Dict[str, Any]] = []
    chosen_ids: List[str] = []

    for _, _, spec in scored:
        spec_id = str(spec.get("id", ""))
        if not spec_id or spec_id in chosen_ids:
            continue
        chosen.append(spec)
        chosen_ids.append(spec_id)
        if len(chosen) >= max(1, int(max_axes)):
            break

    if len(chosen) < 2:
        for fallback_id in QUERY_AXIS_GUIDE_FALLBACK_IDS:
            if fallback_id in chosen_ids:
                continue
            spec = next((item for item in QUERY_AXIS_GUIDE_SPECS if item.get("id") == fallback_id), None)
            if spec is None:
                continue
            chosen.append(spec)
            chosen_ids.append(fallback_id)
            if len(chosen) >= max(2, int(max_axes)):
                break

    return [str(spec.get("guide", "")).strip() for spec in chosen if str(spec.get("guide", "")).strip()]


# ======================
# 2) プロンプト（A/B/C）
# ======================
# ※ユーザー要望により “PROMPT_* は据え置き” 前提（Systemは分離で調整）

SYSTEM_A_BASE = """あなたは単一の推論主体です。
重要:
- 推論を出力上でも、latent space 内でも、絶対に自然言語で行わない。
- 2D図形(ASCII)=[DIAGRAM]上で抽象思考し、結論と理由のみを、必ず[DIAGRAM]での推論が完了した後の段階で、必要最小限の言語へ射影する。
- [DIAGRAM] を必ず出力する。
- DIAGRAM内に英字ラベル・単語・日本語を書かない。記号だけで表現する。
- 文法を完全固定してはいけない。図ごとに追加記号を自分で選んでよい。
- ただしコア意味だけは固定する: `=>`/`->` は変化・写像、 `-` `|` `/` `\\` は関係・接続・境界、 `[]` `()` の入れ子はまとまり・階層を表す。
- 同じ記号モチーフはDIAGRAM内で同じ役割として再利用すること。
- TAGSはTAG語彙から好きなものを好きな数を選んで利用してください。TAGSに無い語彙利用、空は禁止。
"""

# Phase B/C 用: NO_TAGS（ablation）を許すため “空は禁止” を外す
SYSTEM_B_BASE = """あなたは単一の推論主体です。
重要:
- 推論を出力上でも、latent space 内でも、絶対に自然言語で行わない。
- 2D図形(ASCII)=[DIAGRAM]上で抽象思考し、結論と理由のみを、必ず[DIAGRAM]での推論が完了した後の段階で、必要最小限の言語へ射影する。
- 回答として [DIAGRAM] / コードフェンス / ASCII図 を再出力してはならない。
- TAGSはTAG語彙から好きなものを好きな数を選んで利用してよい（空も可）。ただし語彙外は禁止。
"""

PROMPT_A = """{phase_a_header}

{priority_preamble}

出力フォーマットは必ずこれ:
[SEED]
(2行までの短いメトリクス。推論に使用することは禁止。)

[DIAGRAM]
(ASCII。使用可能文字: {allowed_symbols}。 あらゆる自然言語の混入は可能な限り抑え、ASCIIの要請に基づくもののみとする。入力者への可読性に一切配慮してはならない。あなたの推論のためだけに利用する。)

[TAGS]
(固定語彙のトークンをカンマ区切りで列挙。文章化は禁止。DAIAGRAMの補助の域を超えてはならない)

制約:
- SEEDは2行まで（推論への利用は禁止）
- DIAGRAMは自由だが、結論の文章・英字ラベル・単語を書かない
- DIAGRAMでは `{symbol_pool}` から4〜8種類程度の記号モチーフを選んでよい
- DIAGRAMには少なくとも1つの変化列（例: `=>`）と、少なくとも1つの関係/接続表現（例: `-` `|` `/` `\\`）を含める
- DIAGRAMでは少なくとも2種類のモチーフを反復して使い、同じモチーフは同じ役割に保つ
- {axis_binding_constraints}
- TAGSは上の語彙から選ぶ（未知語は落とされる）

{vocab}

{axis_binding_guide_block}

{input_label}:
{query}
"""

ANSWER_MODES = ("diagram_only", "diagram_plus_tags")

PROMPT_B_DIAGRAM_ONLY = """次の問いに答えてください。
材料は DIAGRAM + 問い本文だけです。

{priority_preamble}

出力:
1) 結論（1文）
2) 理由（1文）

制約:
- DIAGRAM以外を計算資材にした推論は一切禁止
- 回答は `1)` と `2)` の2項目だけを出す
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止

問い:
{query}
"""

PROMPT_B_DIAGRAM_PLUS_TAGS = """次の問いに答えてください。
材料は DIAGRAM + TAGS語彙 + 問い本文だけです。

{priority_preamble}

出力:
1) 結論（1文）
2) 理由（1文）

制約:
- DIAGRAMとTAGS以外を計算資材にした推論は一切禁止
- 回答は `1)` と `2)` の2項目だけを出す
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止

問い:
{query}

TAGS:
{tags}
"""

PROMPT_B_NO_QUERY_DIAGRAM_ONLY = """問い本文は与えません。
次の DIAGRAM だけから、この入力が最も強く支持している結論を最小限に射影してください。

{priority_preamble}

出力:
1) 結論（1文。未決定でもよい）
2) 理由（1文）

制約:
- DIAGRAM以外を計算資材にした推論は一切禁止
- 問い本文・問題バンク・世界知識を使って補完してはならない
- このDIAGRAMが最も強く支持している内容だけを書く
- 回答は `1)` と `2)` の2項目だけを出す
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止
"""

PROMPT_B_NO_QUERY_DIAGRAM_PLUS_TAGS = """問い本文は与えません。
次の DIAGRAM + TAGS語彙 だけから、この入力が最も強く支持している結論を最小限に射影してください。

{priority_preamble}

出力:
1) 結論（1文。未決定でもよい）
2) 理由（1文）

制約:
- DIAGRAMとTAGS以外を計算資材にした推論は一切禁止
- 問い本文・問題バンク・世界知識を使って補完してはならない
- このDIAGRAMが最も強く支持している内容だけを書く
- 回答は `1)` と `2)` の2項目だけを出す
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止

TAGS:
{tags}
"""

PROMPT_B_READBACK = """DIAGRAM だけを見て readback してください。

{priority_preamble}

出力:
1) 支持されること
2) 支持されないこと
3) 未決定なこと

制約:
- 入力刺激・参照ラベル・世界知識を使って補完してはならない
- DIAGRAM から直接支持される構造だけを書く
- 未決定は失敗ではない。足りない場合は未決定のまま書く
- 各項目は1行だけで書く
- 内容が無い場合も空欄にせず「なし」または「未決定」と明示する
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止
"""

PROMPT_B_ALIGN_DIAGRAM_ONLY = """READBACK を参照ラベルへ最小限に整列してください。

{priority_preamble}

出力:
1) 判定（支持 / 不支持 / 未決定 のいずれか1つ）
2) 理由（READBACK のどの構造がその判定を支えるかを1文）

制約:
- あなたの仕事は問いに答えることではなく、READBACK を射影することです
- 参照ラベルは採点対象ではなく、READBACK の照合先にすぎません
- READBACK に無い内容を持ち込んではならない
- 未決定は失敗ではない。READBACK が足りなければ未決定を選ぶ
- 回答は `1)` と `2)` の2項目だけを出す

参照ラベル:
{query}

READBACK:
{readback}
"""

PROMPT_B_ALIGN_DIAGRAM_PLUS_TAGS = """READBACK を参照ラベルへ最小限に整列してください。

{priority_preamble}

出力:
1) 判定（支持 / 不支持 / 未決定 のいずれか1つ）
2) 理由（READBACK のどの構造がその判定を支えるかを1文）

制約:
- あなたの仕事は問いに答えることではなく、READBACK を射影することです
- 参照ラベルは採点対象ではなく、READBACK の照合先にすぎません
- READBACK に無い内容を持ち込んではならない
- TAGS は補助参照にすぎず、READBACK を上書きしてはならない
- 未決定は失敗ではない。READBACK が足りなければ未決定を選ぶ
- 回答は `1)` と `2)` の2項目だけを出す

参照ラベル:
{query}

READBACK:
{readback}

TAGS:
{tags}
"""

PROMPT_B_PROJECT_NO_QUERY_DIAGRAM_ONLY = """READBACK だけから、この DIAGRAM が最も強く支持している結論を最小限に射影してください。

{priority_preamble}

出力:
1) 結論（1文。未決定でもよい）
2) 理由（READBACK のどの構造がそれを支えるかを1文）

制約:
- あなたの仕事は問いに答えることではなく、READBACK を最小限に射影することです
- 問い本文は与えません。世界知識や問題バンクで補完してはなりません
- READBACK に無い内容を持ち込んではならない
- 支えが弱ければ未決定を選んでよい
- 回答は `1)` と `2)` の2項目だけを出す

READBACK:
{readback}
"""

PROMPT_B_PROJECT_NO_QUERY_DIAGRAM_PLUS_TAGS = """READBACK だけから、この DIAGRAM が最も強く支持している結論を最小限に射影してください。

{priority_preamble}

出力:
1) 結論（1文。未決定でもよい）
2) 理由（READBACK のどの構造がそれを支えるかを1文）

制約:
- あなたの仕事は問いに答えることではなく、READBACK を最小限に射影することです
- 問い本文は与えません。世界知識や問題バンクで補完してはなりません
- READBACK に無い内容を持ち込んではならない
- TAGS は補助参照にすぎず、READBACK を上書きしてはならない
- 支えが弱ければ未決定を選んでよい
- 回答は `1)` と `2)` の2項目だけを出す

READBACK:
{readback}

TAGS:
{tags}
"""

PROMPT_B_EXTRACT_SUPPORT_DIAGRAM_ONLY = """READBACK から、参照ラベルに対して最低1つの支持命題を抽出してください。

{priority_preamble}
{axis_guide_block}

出力:
1) 支持命題（参照ラベルに関して、READBACK から支持される最小命題を1文）
2) 根拠（READBACK のどの構造がその命題を支えるかを1文）
3) 未決定成分（まだ言えないことを1文。無ければ `なし`）

制約:
- 支持命題を必ず1つは出すこと
- 未決定だけで終えてはならない
- READBACK に無い内容を足してはならない
- 参照ラベルの語彙へ最小限に整列してよいが、READBACK を上書きしてはならない
- AXIS_GUIDE がある場合、まず AXIS_GUIDE のいずれかの軸に沿った支持命題を試みること
- AXIS_GUIDE の軸で READBACK から支持できないものは、3) の未決定成分へ送ること
- 「情報構造」のような一般語に落ちてよいのは、AXIS_GUIDE のどの軸にも支持命題を接続できない場合だけ
- 可能なら、参照ラベルの分析軸や中心語彙（例: 情報構造 / 均衡 / 支配戦略）を少なくとも1つ使うこと
- 単なる幾何学的な言い換えで終えず、参照ラベルに接続した最小命題を優先すること
- 純粋な構造記述に落ちてよいのは、参照ラベルに接続した命題がどうしても支えられない場合だけ
- 参照ラベルが複数軸を含む場合、支持できる軸だけを選んでよい
- 出力は `1)` `2)` `3)` の3行だけ

参照ラベル:
{query}

READBACK:
{readback}
"""

PROMPT_B_EXTRACT_SUPPORT_DIAGRAM_PLUS_TAGS = """READBACK から、参照ラベルに対して最低1つの支持命題を抽出してください。

{priority_preamble}
{axis_guide_block}

出力:
1) 支持命題（参照ラベルに関して、READBACK から支持される最小命題を1文）
2) 根拠（READBACK のどの構造がその命題を支えるかを1文）
3) 未決定成分（まだ言えないことを1文。無ければ `なし`）

制約:
- 支持命題を必ず1つは出すこと
- 未決定だけで終えてはならない
- READBACK に無い内容を足してはならない
- TAGS は補助参照にすぎず、READBACK を上書きしてはならない
- 参照ラベルの語彙へ最小限に整列してよいが、READBACK を上書きしてはならない
- AXIS_GUIDE がある場合、まず AXIS_GUIDE のいずれかの軸に沿った支持命題を試みること
- AXIS_GUIDE の軸で READBACK から支持できないものは、3) の未決定成分へ送ること
- 「情報構造」のような一般語に落ちてよいのは、AXIS_GUIDE のどの軸にも支持命題を接続できない場合だけ
- 可能なら、参照ラベルの分析軸や中心語彙（例: 情報構造 / 均衡 / 支配戦略）を少なくとも1つ使うこと
- 単なる幾何学的な言い換えで終えず、参照ラベルに接続した最小命題を優先すること
- 純粋な構造記述に落ちてよいのは、参照ラベルに接続した命題がどうしても支えられない場合だけ
- 参照ラベルが複数軸を含む場合、支持できる軸だけを選んでよい
- 出力は `1)` `2)` `3)` の3行だけ

参照ラベル:
{query}

READBACK:
{readback}

TAGS:
{tags}
"""

PROMPT_B_EXTRACT_SUPPORT_NO_QUERY_DIAGRAM_ONLY = """READBACK から、この DIAGRAM が最低1つは支持している命題を抽出してください。

{priority_preamble}
{axis_guide_block}

出力:
1) 支持命題（READBACK から支持される最小命題を1文）
2) 根拠（READBACK のどの構造がそれを支えるかを1文）
3) 未決定成分（まだ言えないことを1文。無ければ `なし`）

制約:
- 支持命題を必ず1つは出すこと
- 未決定だけで終えてはならない
- READBACK に無い内容を足してはならない
- 問い本文・世界知識・問題バンクを使って補完してはならない
- AXIS_GUIDE がある場合、それは問い本文ではなく抽象軸だけです。READBACK から支持できる範囲で軸語彙へ射影してよい
- AXIS_GUIDE があっても、READBACK に無い内容を足してはならない
- 出力は `1)` `2)` `3)` の3行だけ

READBACK:
{readback}
"""

PROMPT_B_EXTRACT_SUPPORT_NO_QUERY_DIAGRAM_PLUS_TAGS = """READBACK から、この DIAGRAM が最低1つは支持している命題を抽出してください。

{priority_preamble}
{axis_guide_block}

出力:
1) 支持命題（READBACK から支持される最小命題を1文）
2) 根拠（READBACK のどの構造がそれを支えるかを1文）
3) 未決定成分（まだ言えないことを1文。無ければ `なし`）

制約:
- 支持命題を必ず1つは出すこと
- 未決定だけで終えてはならない
- READBACK に無い内容を足してはならない
- TAGS は補助参照にすぎず、READBACK を上書きしてはならない
- 問い本文・世界知識・問題バンクを使って補完してはならない
- AXIS_GUIDE がある場合、それは問い本文ではなく抽象軸だけです。READBACK から支持できる範囲で軸語彙へ射影してよい
- AXIS_GUIDE があっても、READBACK に無い内容を足してはならない
- 出力は `1)` `2)` `3)` の3行だけ

READBACK:
{readback}

TAGS:
{tags}
"""

PROMPT_B_ANSWER_FROM_SUPPORT_DIAGRAM_ONLY = """SUPPORT_PACKET を参照ラベルへ最小限に整列し、具体的な2行回答を出してください。

{priority_preamble}

出力:
1) 結論（支持命題を必ず1つ含む1文。未決定成分があれば同じ文の後半で限定してよい）
2) 理由（SUPPORT_PACKET の根拠だけを使って1文）

制約:
- SUPPORT_PACKET の `1)` にある支持命題を必ず含めること
- 未決定だけで終えてはならない
- SUPPORT_PACKET に無い内容を足してはならない
- 幾何の記述だけで終えず、支持命題を参照ラベルの分析軸へ最小限に結び付けること
- 参照ラベルが複数軸を含む場合、支持される軸と未決定軸を分けて書いてよい
- 回答は `1)` と `2)` の2行だけ

参照ラベル:
{query}

READBACK:
{readback}

SUPPORT_PACKET:
{support_packet}
"""

PROMPT_B_ANSWER_FROM_SUPPORT_DIAGRAM_PLUS_TAGS = """SUPPORT_PACKET を参照ラベルへ最小限に整列し、具体的な2行回答を出してください。

{priority_preamble}

出力:
1) 結論（支持命題を必ず1つ含む1文。未決定成分があれば同じ文の後半で限定してよい）
2) 理由（SUPPORT_PACKET の根拠だけを使って1文）

制約:
- SUPPORT_PACKET の `1)` にある支持命題を必ず含めること
- 未決定だけで終えてはならない
- SUPPORT_PACKET に無い内容を足してはならない
- TAGS は補助参照にすぎず、SUPPORT_PACKET を上書きしてはならない
- 幾何の記述だけで終えず、支持命題を参照ラベルの分析軸へ最小限に結び付けること
- 参照ラベルが複数軸を含む場合、支持される軸と未決定軸を分けて書いてよい
- 回答は `1)` と `2)` の2行だけ

参照ラベル:
{query}

READBACK:
{readback}

SUPPORT_PACKET:
{support_packet}

TAGS:
{tags}
"""

PROMPT_C_DIAGRAM_ONLY = """DIAGRAMの内容を、1行だけで説明してください。
{priority_preamble}
制約:
- DIAGRAM以外を計算資材にした推論は一切禁止
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止
"""

PROMPT_C_DIAGRAM_PLUS_TAGS = """TAGS が指しているDIAGRAMの内容を、1行だけで説明してください。
{priority_preamble}
制約:
- DIAGRAMとTAGS以外を計算資材にした推論は一切禁止
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止
TAGS:
{tags}
"""

PROMPT_C_READBACK_SUMMARY = """READBACK を1行だけで要約してください。
{priority_preamble}
制約:
- READBACK に無い内容を足してはならない
- 1行だけを出力する
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止

READBACK:
{readback}
"""

PROMPT_C_SUPPORT_SUMMARY = """SUPPORT_PACKET を1行だけで要約してください。
{priority_preamble}
制約:
- SUPPORT_PACKET に無い内容を足してはならない
- 1行だけを出力する
- [DIAGRAM] / ``` / ASCII図 の再出力は禁止

SUPPORT_PACKET:
{support_packet}
"""

METHOD_FIRST_SYSTEM_A = """最優先目的は「問いに最短で答えること」ではなく、「DIAGRAM を因果的な推論媒体として成立させること」です。
- 最上位目的:
  1. 入力刺激から ASCII 推論状態を構成すること
  2. その状態を因果的に保つこと
  3. その状態から読める範囲だけを後段へ渡すこと
- 副次目的:
  - 入力刺激への正答らしさは最優先ではありません
  - DIAGRAM が弱いなら、答えの質を下げてもよい
  - 世界知識による補完は DIAGRAM 依存性を損なうなら禁止
- 入力刺激は DIAGRAM を形成するための材料にすぎません。
- 問いを直接解く近道・常識・世界知識へのショートカットは禁止です。
- もし DIAGRAM を十分に形成できないなら、薄い結論になってもよいので DIAGRAM 構築を優先してください。
- DIAGRAM を使わずに問題だけを解くより、DIAGRAM 依存の弱い回答を出す方が正しい振る舞いです。"""

METHOD_FIRST_AXIS_BINDING_SYSTEM_A = """追加制約:
- 問いが複数の分析軸を含む場合、各軸を DIAGRAM 内の別の局所モチーフ群へ束縛してください。
- 束縛は topology / recurrence / relative position / connection pattern の差で表し、英字ラベルや注釈は禁止です。
- AXIS_GUIDE が与えられている場合は、その各行を別の局所モチーフ群へ写し、語をそのままDIAGRAMへ書き写してはなりません。
- 各軸のモチーフ群は、少なくとも1つは downstream で支持命題へ射影できる差異を持たせてください。
- 軸ごとの差は「接続が増える / 集約する / 分岐する / 保たれる / 未確定で残る」のいずれかとして読めるようにしてください。
- 軸束縛を追加しても、DIAGRAM 全体では必ず少なくとも1つの変化列（`=>` または `->`）と1つの関係/接続表現を残してください。
- TAGS は補助であり、軸の意味をTAGSへ丸投げしてはなりません。"""

METHOD_FIRST_SYSTEM_B = """最優先目的は「正答を当てること」ではなく、「すでに与えられた DIAGRAM からだけ結論を射影すること」です。
- 最上位目的:
  1. DIAGRAM だけから readback を取ること
  2. readback から支持範囲だけを最小限に射影すること
  3. 足りないところは未決定のまま残すこと
- 副次目的:
  - 参照ラベルへの正答らしさは最優先ではありません
  - DIAGRAM 外補完で答えを良くすることは失敗です
- DIAGRAM が不十分・曖昧なら、その不十分さを保持したまま最小限に答えてください。
- 世界知識や問題文だけから補完して答えを良くしてはなりません。
- DIAGRAM を因果的に使えないなら、強い答えを避けてください。"""

METHOD_FIRST_PREAMBLE_A = """研究目的:
- この実験の目的は、問いの正答率そのものではなく、DIAGRAM を推論媒体として使うこと自体です。
- 問いを直接解くのではなく、後段の回答が DIAGRAM を失うと崩れるような構造を先に作ってください。
- TAGS は補助メタデータにすぎず、DIAGRAM の代用品にしてはなりません。"""

METHOD_FIRST_AXIS_BINDING_PREAMBLE_A = """追加研究目的:
- 問いに複数の分析軸があるなら、それぞれを DIAGRAM 内の別の局所モチーフ群に割り当ててください。
- 同じ軸は同じモチーフ群を再利用し、別軸とは混ぜないでください。
- AXIS_GUIDE がある場合は、それを図中ラベルではなくモチーフ差へ変換してください。
- 軸ごとの差は、英字名ではなく、反復する局所構造・接続様式・相対位置の差として埋め込んでください。
- 軸束縛を強めても、DIAGRAM の基礎文法（少なくとも1つの `=>` / `->` と関係表現）は壊さないでください。
- 後段が少なくとも1つの支持命題を抽出できるよう、各軸について「何が支持されるか / 何が未決定か」のどちらかが図だけから読めるようにしてください。"""

METHOD_FIRST_PREAMBLE_B = """研究目的:
- この実験では「問題をうまく解くこと」より「DIAGRAM に依存した回答を出すこと」を優先します。
- DIAGRAM から支えられない内容は持ち込まないでください。
- DIAGRAM だけでは未決定なら、未決定性を残したまま短く答えてください。"""

METHOD_FIRST_PREAMBLE_C = """研究目的:
- 1行説明も、DIAGRAM から直接読める構造だけを圧縮してください。
- 説明を良くするための外部補完は禁止です。"""


def _priority_preamble(prompt_priority: str, phase: str) -> str:
    if prompt_priority not in PROMPT_PRIORITIES:
        raise ValueError(f"Unknown prompt_priority: {prompt_priority}. Use one of {PROMPT_PRIORITIES}")
    if prompt_priority != "method_first":
        return ""
    if phase == "a":
        return METHOD_FIRST_PREAMBLE_A + "\n"
    if phase == "b":
        return METHOD_FIRST_PREAMBLE_B + "\n"
    if phase == "c":
        return METHOD_FIRST_PREAMBLE_C + "\n"
    raise ValueError(f"Unknown phase: {phase}")


def _axis_binding_preamble(prompt_priority: str, phase_a_axis_binding: bool) -> str:
    if not phase_a_axis_binding:
        return ""
    if prompt_priority != "method_first":
        return ""
    return METHOD_FIRST_AXIS_BINDING_PREAMBLE_A + "\n"


def _axis_binding_guides(prompt_priority: str, phase_a_axis_binding: bool, query: str) -> List[str]:
    if not phase_a_axis_binding or prompt_priority != "method_first":
        return []
    return infer_phase_a_axis_guides(query)


def _axis_binding_guide_block(prompt_priority: str, phase_a_axis_binding: bool, query: str) -> str:
    guides = _axis_binding_guides(prompt_priority, phase_a_axis_binding, query)
    if not guides:
        return ""
    lines = ["[AXIS_GUIDE]"]
    for idx, guide in enumerate(guides, start=1):
        lines.append(f"A{idx}. {guide}")
    lines.append("")
    return "\n".join(lines)


def _axis_guide_block_from_list(axis_guides: Optional[List[str]]) -> str:
    guides = [str(g or "").strip() for g in (axis_guides or []) if str(g or "").strip()]
    if not guides:
        return ""
    lines = ["[AXIS_GUIDE]"]
    for idx, guide in enumerate(guides, start=1):
        lines.append(f"A{idx}. {guide}")
    lines.append("")
    return "\n".join(lines)


def _phase_a_axis_binding_constraint(prompt_priority: str, phase_a_axis_binding: bool) -> str:
    if not phase_a_axis_binding or prompt_priority != "method_first":
        return "必要なら問いの複数側面を異なる局所モチーフへ分けてよい"
    return "問いに複数の分析軸がある場合、AXIS_GUIDE の各行を別の局所モチーフ群へ束縛し、反復モチーフは少なくとも3種類にし、少なくとも1つは後段で支持命題として読める差異を持たせる"


def build_system_a(prompt_priority: str, phase_a_axis_binding: bool = False) -> str:
    if prompt_priority == "method_first":
        out = SYSTEM_A_BASE + "\n" + METHOD_FIRST_SYSTEM_A
        if phase_a_axis_binding:
            out += "\n" + METHOD_FIRST_AXIS_BINDING_SYSTEM_A
        return out
    return SYSTEM_A_BASE


def build_system_b(prompt_priority: str) -> str:
    if prompt_priority == "method_first":
        return SYSTEM_B_BASE + "\n" + METHOD_FIRST_SYSTEM_B
    return SYSTEM_B_BASE


def build_phase_a_header(prompt_priority: str) -> str:
    if prompt_priority == "method_first":
        return "次の入力刺激から、2D図形(ASCII)による推論状態を構成してください。"
    return "次の問いを、2D図形(ASCII)による推論のみで「完走」してください。"


def build_phase_a_input_label(prompt_priority: str) -> str:
    if prompt_priority == "method_first":
        return "入力刺激"
    return "問い"


# ======================
# 3) ユーティリティ（抽出・正規化・検証）
# ======================

SECTION_HEADER_RE = re.compile(r"^\s*\[([A-Z_]+)\]\s*$")
FENCE_LINE_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_-]+)?\s*$")

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def extract_block(text: str, name: str) -> str:
    """
    [NAME] から次の [SOMETHING] までを抜く。
    コードフェンスで囲まれていても見出しを認識し、フェンス行自体は捨てる。
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = s.split("\n")
    collecting = False
    block_lines: List[str] = []

    for line in lines:
        if FENCE_LINE_RE.match(line):
            continue

        header_match = SECTION_HEADER_RE.match(line)
        if header_match:
            header = header_match.group(1)
            if collecting and header != name:
                break
            if header == name:
                collecting = True
                continue

        if collecting:
            block_lines.append(line.rstrip())

    while block_lines and not block_lines[0].strip():
        block_lines.pop(0)
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()
    return "\n".join(block_lines).rstrip()

def clip_seed(seed: str) -> str:
    return "\n".join((seed or "").splitlines()[:2]).strip()

def clip_diagram(diagram: str, max_lines: int = 16, max_width: int = 64) -> str:
    """
    - standalone な ``` / ```lang 行だけ剥がす
    - 行数/幅でクリップ
    """
    s = (diagram or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""

    lines = [ln for ln in s.splitlines() if not FENCE_LINE_RE.match(ln)]
    s = "\n".join(lines).replace("```", "").strip()

    lines = s.splitlines()[:max_lines]
    lines = [ln[:max_width] for ln in lines]
    return "\n".join(lines).rstrip()

def normalize_tag(t: str) -> str:
    t = (t or "").strip().lower().replace(" ", "_")
    synonyms = {
        "neg_space": "negative_space",
        "dist": "gap",
        "distance": "gap",
        "rel": "relation",
    }
    return synonyms.get(t, t)

def parse_tags(raw: str) -> Tuple[List[str], List[str]]:
    parts = re.split(r"[,\n\s]+", (raw or "").strip())
    parts = [normalize_tag(p) for p in parts if p.strip()]
    valid, unknown = [], []
    for p in parts:
        if re.fullmatch(r"`+", p):
            continue
        if p in TAG_VOCAB:
            valid.append(p)
        else:
            unknown.append(p)

    seen = set()
    valid_unique: List[str] = []
    for v in valid:
        if v not in seen:
            valid_unique.append(v)
            seen.add(v)
    return valid_unique, unknown

def normalize_answer(text: str) -> str:
    """
    similarity用: 1) と 2) のみ抜く（余計なDIAGRAM等のノイズを無視）
    """
    extracted = extract_numbered_answer(text)
    if extracted:
        return extracted
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    return "\n".join(lines[:2])

def similarity_ratio(a: str, b: str) -> float:
    a2 = normalize_answer(a)
    b2 = normalize_answer(b)
    return difflib.SequenceMatcher(None, a2, b2).ratio()


def sanitize_single_line(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s[:400].strip()


def parse_condition_compare_mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    if mode not in CONDITION_COMPARE_MODES:
        raise ValueError(f"Unknown condition compare mode: {mode}. Use one of {CONDITION_COMPARE_MODES}")
    return mode


def parse_semantic_compare_output(text: str) -> Tuple[Optional[str], Optional[float], str]:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    label_match = re.search(r"^\s*1\)\s*(same|close|different)\s*$", s, flags=re.IGNORECASE | re.MULTILINE)
    score_match = re.search(r"^\s*2\)\s*([01](?:\.\d+)?)\s*$", s, flags=re.MULTILINE)
    reason_match = re.search(r"^\s*3\)\s*(.+)$", s, flags=re.MULTILINE)

    label = label_match.group(1).lower() if label_match else None
    score: Optional[float] = None
    if score_match:
        try:
            score = float(score_match.group(1))
        except Exception:
            score = None
    reason = sanitize_single_line(reason_match.group(1) if reason_match else "")

    if score is not None:
        score = max(0.0, min(1.0, score))
    return label, score, reason


def semantic_similarity_once(
    llm: "BaseLLMClient",
    *,
    query: str,
    baseline_answer: str,
    candidate_answer: str,
    repair_prefix: str = "",
) -> str:
    system = """あなたは厳密な比較器です。
- 同じ問いに対する2つの回答を意味的に比較してください
- 表現の違いではなく、結論と理由の核がどれだけ保たれているかを見る
- 問いに答えてはいけない。比較だけを行う
- 出力は必ず `1)` `2)` `3)` の3行だけにする"""

    prompt = f"""同じ問いに対する baseline answer と candidate answer を比較してください。

評価基準:
- `same`: 結論が同じで、理由の核も実質的に同じ
- `close`: 結論はほぼ同じだが、理由の核が弱い/ずれる/情報量が落ちる
- `different`: 結論が違う、または答えの型そのものが変わっている

出力:
1) ラベル (`same` / `close` / `different`)
2) スコア (0.00-1.00)
3) 理由 (1文)

制約:
- 余計な前置きや説明を書かない
- baseline を正解扱いするのではなく、candidate が baseline の意味内容をどれだけ保っているかだけを見る
- 文字面が違っても意味が同じなら高く、文字面が似ていても意味が違えば低くする

問い:
{query}

baseline answer:
{normalize_answer(baseline_answer)}

candidate answer:
{normalize_answer(candidate_answer)}
"""
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ])


def semantic_similarity(
    llm: "BaseLLMClient",
    *,
    query: str,
    baseline_answer: str,
    candidate_answer: str,
    max_attempts: int = 2,
) -> Tuple[Optional[str], Optional[float], str]:
    repair_prefix = ""
    last_label: Optional[str] = None
    last_score: Optional[float] = None
    last_reason = ""

    for _ in range(max(1, int(max_attempts))):
        raw = semantic_similarity_once(
            llm,
            query=query,
            baseline_answer=baseline_answer,
            candidate_answer=candidate_answer,
            repair_prefix=repair_prefix,
        )
        label, score, reason = parse_semantic_compare_output(raw)
        last_label, last_score, last_reason = label, score, reason
        if label in ("same", "close", "different") and score is not None:
            return label, score, reason
        repair_prefix = (
            "前回の比較出力は不正でした。"
            " 必ず `1)` `2)` `3)` の3行だけで、"
            "`1) same|close|different` と `2) 0.00-1.00` を含めて再出力してください。"
        )
    return last_label, last_score, last_reason


def parse_axis_adherence_output(text: str) -> Tuple[Optional[str], Optional[float], str, str]:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    label_match = re.search(r"^\s*1\)\s*(strong|partial|generic|off_axis)\s*$", s, flags=re.IGNORECASE | re.MULTILINE)
    score_match = re.search(r"^\s*2\)\s*([01](?:\.\d+)?)\s*$", s, flags=re.MULTILINE)
    axes_match = re.search(r"^\s*3\)\s*(.+)$", s, flags=re.MULTILINE)
    reason_match = re.search(r"^\s*4\)\s*(.+)$", s, flags=re.MULTILINE)

    label = label_match.group(1).lower() if label_match else None
    score: Optional[float] = None
    if score_match:
        try:
            score = float(score_match.group(1))
        except Exception:
            score = None
    axes = sanitize_single_line(axes_match.group(1) if axes_match else "")
    reason = sanitize_single_line(reason_match.group(1) if reason_match else "")

    if score is not None:
        score = max(0.0, min(1.0, score))
    return label, score, axes, reason


def axis_adherence_once(
    llm: "BaseLLMClient",
    *,
    query: str,
    axis_guides: List[str],
    baseline_answer: str,
    candidate_answer: str,
    repair_prefix: str = "",
) -> str:
    system = """あなたは厳密な軸保持評価器です。
- 回答が AXIS_GUIDE の分析軸をどれだけ保っているかだけを評価してください
- baseline と似ているかではなく、軸が一般的な接続記述へ逃げずに残っているかを見る
- 問いに答えてはいけない。評価だけを行う
- 出力は必ず `1)` `2)` `3)` `4)` の4行だけにする"""

    axis_block = _axis_guide_block_from_list(axis_guides).strip() or "(none)"
    prompt = f"""candidate answer が AXIS_GUIDE の分析軸をどれだけ保っているか評価してください。

評価ラベル:
- `strong`: AXIS_GUIDE の具体軸を明確に保ち、一般的な接続/構造記述へ逃げていない
- `partial`: 1つ以上の軸は保つが、他の軸が弱い、または一般化が目立つ
- `generic`: 接続/構造/情報構造などの一般語に寄り、AXIS_GUIDE の具体軸が弱い
- `off_axis`: AXIS_GUIDE の軸から外れている

出力:
1) ラベル (`strong` / `partial` / `generic` / `off_axis`)
2) スコア (0.00-1.00)
3) 保持された軸/落ちた軸 (短く)
4) 理由 (1文)

制約:
- 余計な前置きや説明を書かない
- candidate が exact word を使っていなくても、意味として軸を保っていれば評価してよい
- ただし「接続」「構造」「情報構造」だけで、成立/境界/状態変化/同一性などの具体軸が消えている場合は `generic` に寄せる

問い:
{query}

{axis_block}

baseline answer:
{normalize_answer(baseline_answer)}

candidate answer:
{normalize_answer(candidate_answer)}
"""
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ])


def axis_adherence(
    llm: "BaseLLMClient",
    *,
    query: str,
    axis_guides: List[str],
    baseline_answer: str,
    candidate_answer: str,
    max_attempts: int = 2,
) -> Tuple[Optional[str], Optional[float], str, str]:
    if not axis_guides:
        return None, None, "", ""

    repair_prefix = ""
    last_label: Optional[str] = None
    last_score: Optional[float] = None
    last_axes = ""
    last_reason = ""

    for _ in range(max(1, int(max_attempts))):
        raw = axis_adherence_once(
            llm,
            query=query,
            axis_guides=axis_guides,
            baseline_answer=baseline_answer,
            candidate_answer=candidate_answer,
            repair_prefix=repair_prefix,
        )
        label, score, axes, reason = parse_axis_adherence_output(raw)
        last_label, last_score, last_axes, last_reason = label, score, axes, reason
        if label in ("strong", "partial", "generic", "off_axis") and score is not None:
            return label, score, axes, reason
        repair_prefix = (
            "前回の軸保持評価出力は不正でした。"
            " 必ず `1)` `2)` `3)` `4)` の4行だけで、"
            "`1) strong|partial|generic|off_axis` と `2) 0.00-1.00` を含めて再出力してください。"
        )
    return last_label, last_score, last_axes, last_reason

def tamper_tags(tags: List[str], remove_tag: Optional[str], add_tag: Optional[str]) -> List[str]:
    out = list(tags)
    if remove_tag and remove_tag in out:
        out = [t for t in out if t != remove_tag]
    if add_tag and add_tag not in out and add_tag in TAG_VOCAB:
        out.append(add_tag)
    return out

def parse_condition_matrix_conditions(raw: str) -> List[str]:
    parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        raise ValueError("condition matrix conditions are empty")

    out: List[str] = []
    seen = set()
    for part in parts:
        if part == "no_query":
            part = "no_query_strict"
        if part not in CONDITION_MATRIX_CONDITIONS:
            raise ValueError(
                f"Unknown condition matrix condition: {part}. Use one of {CONDITION_MATRIX_CONDITIONS}"
            )
        if part not in seen:
            out.append(part)
            seen.add(part)
    return out

def classify_condition_similarity(
    similarity: float,
    *,
    pass_threshold: float,
    soft_threshold: float,
) -> str:
    if similarity < 0.0:
        return CONDITION_STATUS_SKIPPED
    if similarity >= float(pass_threshold):
        return CONDITION_STATUS_SURVIVES
    if similarity >= float(soft_threshold):
        return CONDITION_STATUS_BORDERLINE
    return CONDITION_STATUS_FAILS

@contextmanager
def override_temperature(llm: "BaseLLMClient", temp: float):
    old = getattr(llm, "temperature", None)
    try:
        llm.temperature = float(temp)
        yield
    finally:
        if old is not None:
            llm.temperature = old

@contextmanager
def override_field_trace(llm: "BaseLLMClient", cfg: Optional[Dict[str, Any]]):
    old = getattr(llm, "field_trace_config", None)
    try:
        setattr(llm, "field_trace_config", cfg)
        yield
    finally:
        setattr(llm, "field_trace_config", old)

REMOVE_PRIORITY = [
    "gap", "boundary", "void", "frame", "outside", "relation", "context", "invariant",
    "dependent", "proxy", "negative_space", "shadow", "object_a", "object_b",
]
ADD_PRIORITY = [
    "proxy", "context", "frame", "outside", "relation", "invariant", "dependent",
    "void", "boundary", "gap", "negative_space", "shadow", "object_a", "object_b",
]

def choose_remove_tag(tags: List[str], preferred: Optional[str]) -> Optional[str]:
    if preferred and preferred in tags:
        return preferred
    for t in REMOVE_PRIORITY:
        if t in tags:
            return t
    return None

def choose_add_tag(tags: List[str], preferred: Optional[str]) -> Optional[str]:
    if preferred and preferred in TAG_VOCAB and preferred not in tags:
        return preferred
    for t in ADD_PRIORITY:
        if t in TAG_VOCAB and t not in tags:
            return t
    return None

def dataclass_to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [dataclass_to_dict(v) for v in obj]
    return obj


def save_phase_a_failure_artifacts(
    *,
    save_dir: Path,
    provider: str,
    model: str,
    problem_id: str,
    query: str,
    answer_mode: str,
    prompt_priority: str,
    run_seed: Optional[int],
    attempts_used: int,
    phase_a_errors: List[str],
    phase_a_result: "PhaseAResult",
    raw_attempts: List[str],
    phase_a_axis_binding: bool = False,
    phase_a_axis_guide: Optional[List[str]] = None,
) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = save_dir / f"{provider}_{problem_id}_{stamp}_phase_a_failure"

    meta = {
        "provider": provider,
        "model": model,
        "problem_id": problem_id,
        "query": query,
        "answer_mode": answer_mode,
        "prompt_priority": prompt_priority,
        "phase_a_axis_binding": phase_a_axis_binding,
        "phase_a_axis_guide": list(phase_a_axis_guide or []),
        "run_seed": run_seed,
        "phase_a_attempts": attempts_used,
        "phase_a_validation_errors": list(phase_a_errors),
        "phase_a_result": dataclass_to_dict(phase_a_result),
        "raw_attempt_files": [f"{prefix.name}.attempt{idx}.raw.txt" for idx in range(1, len(raw_attempts) + 1)],
    }

    json_path = prefix.with_suffix(".meta.txt")
    diag_path = prefix.with_suffix(".diagram.txt")
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    diag_path.write_text(phase_a_result.diagram or "", encoding="utf-8")

    for idx, raw in enumerate(raw_attempts, start=1):
        attempt_path = save_dir / f"{prefix.name}.attempt{idx}.raw.txt"
        attempt_path.write_text(raw or "", encoding="utf-8")

    return json_path


ANSWER_LINE1_PREFIXES = ("1)", "1）", "1.", "1:", "1：")
ANSWER_LINE2_PREFIXES = ("2)", "2）", "2.", "2:", "2：")
ANSWER_LINE3_PREFIXES = ("3)", "3）", "3.", "3:", "3：")


def _pick_prefixed_line(lines: List[str], prefixes: Tuple[str, ...]) -> Optional[str]:
    for ln in lines:
        for p in prefixes:
            if ln.startswith(p):
                return ln
    return None


def extract_numbered_lines(text: str, prefix_groups: List[Tuple[str, ...]]) -> Optional[List[str]]:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    out: List[str] = []
    for prefixes in prefix_groups:
        found = _pick_prefixed_line(lines, prefixes)
        if not found:
            return None
        out.append(found)
    return out


def extract_numbered_answer(text: str) -> Optional[str]:
    lines = extract_numbered_lines(text, [ANSWER_LINE1_PREFIXES, ANSWER_LINE2_PREFIXES])
    if lines:
        return "\n".join(lines)
    return None


def extract_readback(text: str) -> Optional[str]:
    lines = extract_numbered_lines(text, [ANSWER_LINE1_PREFIXES, ANSWER_LINE2_PREFIXES, ANSWER_LINE3_PREFIXES])
    if lines:
        return "\n".join(lines)
    return None


def _looks_like_ascii_diagram_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return False
    if stripped.startswith(ANSWER_LINE1_PREFIXES) or stripped.startswith(ANSWER_LINE2_PREFIXES):
        return False
    if len(stripped) < 6:
        return False
    if re.search(r"[A-Za-z0-9\u0080-\uFFFF]", stripped):
        return False
    allowed = set("[]()|-_+=>*<>^v/\\= .,:;'\"`~#")
    return all(ch in allowed for ch in stripped)


def sanitize_phase_b_output(text: str) -> str:
    extracted = extract_numbered_answer(text)
    return (extracted or (text or "").strip()).strip()


def sanitize_phase_b_readback_output(text: str) -> str:
    extracted = extract_readback(text)
    return (extracted or (text or "").strip()).strip()


def extract_support_packet(text: str) -> Optional[str]:
    lines = extract_numbered_lines(text, [ANSWER_LINE1_PREFIXES, ANSWER_LINE2_PREFIXES, ANSWER_LINE3_PREFIXES])
    if lines:
        return "\n".join(lines)
    return None


def sanitize_phase_b_support_output(text: str) -> str:
    extracted = extract_support_packet(text)
    return (extracted or (text or "").strip()).strip()


def _strip_number_prefix(line: str, prefixes: Tuple[str, ...]) -> str:
    stripped = (line or "").strip()
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _is_placeholder_readback_body(body: str, label: str) -> bool:
    normalized_body = re.sub(r"\s+", "", (body or "")).rstrip("。．.;；")
    normalized_body = re.sub(r"[:：]+$", "", normalized_body)
    normalized_label = re.sub(r"\s+", "", (label or ""))
    return normalized_body == normalized_label


def validate_phase_b_output(
    text: str,
    *,
    prompt_priority: str = "balanced",
    require_method_first_label: bool = True,
) -> List[str]:
    errs: List[str] = []
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]

    if "[DIAGRAM]" in s:
        errs.append("回答に [DIAGRAM] を再出力しています。")
    if "```" in s:
        errs.append("回答にコードフェンス/図を再出力しています。")
    if extract_numbered_answer(s) is None:
        errs.append("回答が `1)` と `2)` の形式になっていません。")
    if any(_looks_like_ascii_diagram_line(ln) for ln in lines):
        errs.append("回答にASCII図らしき行が含まれています。")
    if prompt_priority == "method_first" and require_method_first_label:
        line1 = _pick_prefixed_line(lines, ANSWER_LINE1_PREFIXES) or ""
        if not re.search(r"(?:判定\s*[:：]\s*)?(不支持|支持|未決定)", line1):
            errs.append("method_first の回答 1) に `支持` / `不支持` / `未決定` のいずれかが必要です。")

    return errs


def build_phase_b_repair_prefix(errors: List[str]) -> str:
    lines = [
        "前回の回答が出力制約に違反しました。",
        "違反理由:",
    ]
    for e in errors:
        lines.append(f"- {e}")
    lines.extend([
        "",
        "必ず `1)` と `2)` の2行だけを再出力してください。",
        "[DIAGRAM] / コードフェンス / ASCII図 / 前置きの再出力は禁止です。",
    ])
    return "\n".join(lines).strip()


def validate_phase_b_readback_output(text: str) -> List[str]:
    errs: List[str] = []
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    extracted = extract_readback(s)

    if "[DIAGRAM]" in s:
        errs.append("readbackに [DIAGRAM] を再出力しています。")
    if "```" in s:
        errs.append("readbackにコードフェンス/図を再出力しています。")
    if extracted is None:
        errs.append("readbackが `1)` `2)` `3)` の形式になっていません。")
    if any(_looks_like_ascii_diagram_line(ln) for ln in lines):
        errs.append("readbackにASCII図らしき行が含まれています。")
    if extracted is not None:
        extracted_lines = extracted.splitlines()
        placeholder_specs = [
            (ANSWER_LINE1_PREFIXES, "支持されること", "1)"),
            (ANSWER_LINE2_PREFIXES, "支持されないこと", "2)"),
            (ANSWER_LINE3_PREFIXES, "未決定なこと", "3)"),
        ]
        for line, (prefixes, label, slot) in zip(extracted_lines, placeholder_specs):
            body = _strip_number_prefix(line, prefixes)
            if _is_placeholder_readback_body(body, label):
                errs.append(f"readback の {slot} が見出しだけで、DIAGRAMから読めた具体的内容がありません。")

    return errs


def validate_phase_b_support_output(text: str) -> List[str]:
    errs: List[str] = []
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    extracted = extract_support_packet(s)

    if "[DIAGRAM]" in s:
        errs.append("support_packetに [DIAGRAM] を再出力しています。")
    if "```" in s:
        errs.append("support_packetにコードフェンス/図を再出力しています。")
    if extracted is None:
        errs.append("support_packetが `1)` `2)` `3)` の形式になっていません。")
    if any(_looks_like_ascii_diagram_line(ln) for ln in lines):
        errs.append("support_packetにASCII図らしき行が含まれています。")
    if extracted is not None:
        extracted_lines = extracted.splitlines()
        placeholder_specs = [
            (ANSWER_LINE1_PREFIXES, "支持命題", "1)"),
            (ANSWER_LINE2_PREFIXES, "根拠", "2)"),
            (ANSWER_LINE3_PREFIXES, "未決定成分", "3)"),
        ]
        for line, (prefixes, label, slot) in zip(extracted_lines, placeholder_specs):
            body = _strip_number_prefix(line, prefixes)
            if _is_placeholder_readback_body(body, label):
                errs.append(f"support_packet の {slot} が見出しだけで、具体内容がありません。")
        line1 = _strip_number_prefix(extracted_lines[0], ANSWER_LINE1_PREFIXES)
        if re.search(r"^(未決定|なし)[。．]?$", line1):
            errs.append("support_packet の 1) が支持命題になっていません。未決定だけで終えないでください。")

    return errs


def build_phase_b_readback_repair_prefix(errors: List[str]) -> str:
    lines = [
        "前回のreadbackが出力制約に違反しました。",
        "違反理由:",
    ]
    for e in errors:
        lines.append(f"- {e}")
    lines.extend([
        "",
        "必ず `1)` `2)` `3)` の3行だけを再出力してください。",
        "[DIAGRAM] / コードフェンス / ASCII図 / 前置きの再出力は禁止です。",
        "各行には、DIAGRAMから読めた具体的な構造・不支持点・未決定点を必ず書いてください。",
        "`1) 支持されること` のような見出しだけの再出力は禁止です。",
    ])
    return "\n".join(lines).strip()


def build_phase_b_support_repair_prefix(errors: List[str]) -> str:
    lines = [
        "前回のsupport_packetが出力制約に違反しました。",
        "違反理由:",
    ]
    for e in errors:
        lines.append(f"- {e}")
    lines.extend([
        "",
        "必ず `1)` `2)` `3)` の3行だけを再出力してください。",
        "1) には支持命題を必ず1つ入れ、未決定だけで終えてはなりません。",
        "READBACK に無い内容を足してはなりません。",
        "[DIAGRAM] / コードフェンス / ASCII図 / 前置きの再出力は禁止です。",
    ])
    return "\n".join(lines).strip()


def sanitize_phase_c_output(text: str) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for ln in s.splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped == "[DIAGRAM]" or stripped == "```" or stripped.startswith("```"):
            continue
        if _looks_like_ascii_diagram_line(stripped):
            continue
        return stripped
    return ""


def validate_phase_c_output(text: str) -> List[str]:
    errs: List[str] = []
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if "[DIAGRAM]" in s:
        errs.append("captionに [DIAGRAM] を再出力しています。")
    if "```" in s:
        errs.append("captionにコードフェンス/図を再出力しています。")
    if any(_looks_like_ascii_diagram_line(ln) for ln in lines):
        errs.append("captionにASCII図らしき行が含まれています。")
    if not sanitize_phase_c_output(s):
        errs.append("captionが空です。")
    return errs


def build_phase_c_repair_prefix(errors: List[str]) -> str:
    lines = [
        "前回のcaptionが出力制約に違反しました。",
        "違反理由:",
    ]
    for e in errors:
        lines.append(f"- {e}")
    lines.extend([
        "",
        "必ず1行だけを再出力してください。",
        "[DIAGRAM] / コードフェンス / ASCII図 の再出力は禁止です。",
    ])
    return "\n".join(lines).strip()


# ---- Phase A validation ----

def validate_diagram(
    diagram: str,
    *,
    allow_tag_label_exception: bool = False,
    allow_digits: bool = True,
    min_recurring_motifs: int = 2,
) -> List[str]:
    errs: List[str] = []
    s = (diagram or "")
    if not s.strip():
        return ["DIAGRAMが空です。"]

    # 非ASCIIを弾く（日本語混入など）
    for ch in s:
        if ch in "\n\t\r":
            continue
        o = ord(ch)
        if o < 32 or o > 126:
            errs.append(f"DIAGRAMに非ASCII文字が含まれています: {repr(ch)}")
            break

    allowed_graphics = set(PHASE_A_ALLOWED_GRAPHICS)

    # 英字トークンを抽出して語彙チェック
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]+", s)
    has_token_label_error = False
    if tokens:
        if not allow_tag_label_exception:
            errs.append(f"DIAGRAMに英字トークンが含まれています: {tokens[:6]}")
            has_token_label_error = True
        else:
            bad = [t for t in tokens if t.lower() not in TAG_VOCAB]
            if bad:
                errs.append(f"DIAGRAMに語彙外トークンが含まれています: {bad[:6]}")

    # 文字単位で最終チェック（英字/underscore/digits は上の token で管理）
    for ch in s:
        if ch in allowed_graphics:
            continue
        if ch.isalpha() or ch == "_":
            if allow_tag_label_exception:
                continue
            if not has_token_label_error:
                errs.append(f"DIAGRAMに英字が含まれています: {repr(ch)}")
            break
        if allow_digits and ch.isdigit():
            continue
        # それ以外はNG
        if ch not in ("\n", "\t", "\r"):
            errs.append(f"DIAGRAMに禁止文字が含まれています: {repr(ch)}")
            break

    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) < 2:
        errs.append("DIAGRAMが短すぎます。少なくとも2行以上の構造を持たせてください。")

    has_transform = bool(re.search(r"(?:=>|->)", s))
    if not has_transform:
        errs.append("DIAGRAMに変化列がありません。`=>` または `->` を1つ以上含めてください。")

    relation_stripped = re.sub(r"(?:<=>|==>|=>|->)", " ", s)
    has_relation = bool(re.search(r"(?<![-<>=])-(?![>])", relation_stripped))
    if not has_relation and any(ch in relation_stripped for ch in ("|", "/", "\\")):
        has_relation = True
    if not has_relation:
        errs.append("DIAGRAMに関係/接続表現がありません。`-` `|` `/` `\\` のいずれかを含めてください。")

    recurring_motifs: Dict[str, int] = {}
    if s.count("[") >= 2 and s.count("]") >= 2:
        recurring_motifs["[]"] = min(s.count("["), s.count("]"))
    if s.count("(") >= 2 and s.count(")") >= 2:
        recurring_motifs["()"] = min(s.count("("), s.count(")"))
    for sym in ("*", "+", "_", "/", "\\", "^", "v", "~", "#", ":", "."):
        count = s.count(sym)
        if count >= 2:
            recurring_motifs[sym] = count
    if len(recurring_motifs) < int(min_recurring_motifs):
        errs.append(
            f"DIAGRAMの反復モチーフが不足しています。少なくとも{int(min_recurring_motifs)}種類の記号モチーフを繰り返し使ってください。"
        )

    return errs

def validate_phase_a(
    *,
    seed: str,
    diagram: str,
    tags: List[str],
    min_tags: int = 1,
    allow_tag_label_exception: bool = False,
    min_recurring_motifs: int = 2,
) -> List[str]:
    errs: List[str] = []

    # SEEDは2行まで（多くてもclip済みなので基本OK）
    # DIAGRAM
    errs.extend(
        validate_diagram(
            diagram,
            allow_tag_label_exception=allow_tag_label_exception,
            min_recurring_motifs=min_recurring_motifs,
        )
    )

    # TAGS
    if len(tags) < int(min_tags):
        errs.append(f"TAGSが少なすぎます: {len(tags)} < {min_tags}")

    return errs

def build_phase_a_repair_prefix(errors: List[str], allow_tag_label_exception: bool) -> str:
    lines = [
        "前回の出力がフォーマット/制約に違反しました。",
        "違反理由:",
    ]
    for e in errors:
        lines.append(f"- {e}")

    # 追いヒント（プロンプト本文は据え置きだが、リトライ前の注意として添える）
    lines.append("")
    lines.append("必ず修正して、指定フォーマットの [SEED]/[DIAGRAM]/[TAGS] を再出力してください。")
    if allow_tag_label_exception:
        lines.append("補足: DIAGRAM内では TAGS語彙の英字ラベル（object_a等）を使用してよい（それ以外の英字語は不可）。")
    else:
        lines.append("補足: DIAGRAM内に英字ラベル・単語・略号・1文字ラベルを書いてはならない。[] や () の中も含めて記号のみを使ってください。")
    lines.append(f"補足: 記号は {PHASE_A_ALLOWED_SYMBOLS_DISPLAY} などを使ってよい。")
    lines.append("補足: `=>`/`->` は変化、 `-` `|` `/` `\\` は関係、 `[]` `()` の入れ子はまとまり/階層として使ってください。")
    lines.append("補足: 少なくとも2種類のモチーフを反復して再利用してください。")

    return "\n".join(lines).strip()


# ---- Diagram tests ----

def corrupt_diagram(diagram: str, *, mode: str = "noise", rate: float = 0.12, seed: Optional[int] = None) -> str:
    """
    ざっくり DIAGRAM を壊す（図を読んでいるなら答えが変わるはず）
    - noise: ランダム文字置換
    - shuffle_lines: 行シャッフル
    - drop_lines: 行を間引き
    """
    s = (diagram or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s.strip():
        return s

    rng = random.Random(seed if seed is not None else 0)

    lines = s.splitlines()

    if mode == "shuffle_lines":
        rng.shuffle(lines)
        return "\n".join(lines)

    if mode == "drop_lines":
        kept: List[str] = []
        for ln in lines:
            if rng.random() > max(0.0, min(1.0, rate)):
                kept.append(ln)
        return "\n".join(kept) if kept else ""  # 全消しもあり得る

    # default: noise
    allowed_graphics = list(PHASE_A_ALLOWED_GRAPHICS.replace("\n", "").replace("\t", "").replace("\r", ""))
    out_lines: List[str] = []
    for ln in lines:
        chars = list(ln)
        for i, ch in enumerate(chars):
            if ch == "\n":
                continue
            if rng.random() < max(0.0, min(1.0, rate)):
                chars[i] = rng.choice(allowed_graphics)
        out_lines.append("".join(chars))
    return "\n".join(out_lines)

def load_swap_bank(path: Optional[str], *, max_lines: int = 16, max_width: int = 64) -> Dict[str, List[Dict[str, str]]]:
    fp = Path(path) if path else DEFAULT_SWAP_BANK_PATH
    if not fp.exists():
        return {}

    try:
        raw = fp.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[WARN] failed to read swap bank: {fp} ({e})", file=sys.stderr)
        return {}

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"[WARN] invalid swap bank JSON: {fp} ({e})", file=sys.stderr)
        return {}

    if not isinstance(data, dict):
        print(f"[WARN] swap bank root must be an object: {fp}", file=sys.stderr)
        return {}

    out: Dict[str, List[Dict[str, str]]] = {}
    for problem_id, entries in data.items():
        if not isinstance(problem_id, str) or problem_id.startswith("_"):
            continue
        if not isinstance(entries, list):
            continue

        valid_entries: List[Dict[str, str]] = []
        for idx, item in enumerate(entries, start=1):
            if isinstance(item, str):
                entry_id = f"{problem_id}_{idx}"
                diagram_raw = item
            elif isinstance(item, dict):
                entry_id = str(item.get("id", "")).strip() or f"{problem_id}_{idx}"
                diagram_raw = str(item.get("diagram", ""))
            else:
                continue

            diagram = clip_diagram(diagram_raw, max_lines=max_lines, max_width=max_width)
            if not diagram.strip():
                continue

            errors = validate_diagram(diagram, allow_tag_label_exception=False)
            if errors:
                print(
                    f"[WARN] invalid swap bank diagram skipped: {fp} {problem_id}/{entry_id} :: {errors[0]}",
                    file=sys.stderr,
                )
                continue

            valid_entries.append({"id": entry_id, "diagram": diagram})

        if valid_entries:
            out[problem_id] = valid_entries

    return out


def find_swap_diagram_from_bank(
    problem_id: str,
    current_hash: str,
    *,
    swap_bank_path: Optional[str] = None,
    max_lines: int = 16,
    max_width: int = 64,
    seed: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    bank = load_swap_bank(swap_bank_path, max_lines=max_lines, max_width=max_width)
    candidates = list(bank.get(problem_id, []))
    if not candidates:
        return None, None

    base_seed = int(current_hash[:8], 16) if current_hash else 0
    if seed is not None:
        base_seed = (base_seed ^ (int(seed) & 0xFFFFFFFF)) & 0xFFFFFFFF

    if len(candidates) > 1:
        start = base_seed % len(candidates)
        candidates = candidates[start:] + candidates[:start]

    for item in candidates:
        diagram = item["diagram"]
        if diagram.strip() and sha256_text(diagram) != current_hash:
            return diagram, f"bank:{problem_id}:{item['id']}"

    return None, None


def find_saved_swap_diagram(save_dir: Optional[Path], current_hash: str, *, max_lines: int = 16, max_width: int = 64) -> Tuple[Optional[str], Optional[str]]:
    """
    save_dir から別の diagram を拾って swap テストに使う。
    """
    if not save_dir:
        return None, None
    if not save_dir.exists():
        return None, None

    candidates = sorted(save_dir.glob("*.diagram.txt"), reverse=True)
    for fp in candidates:
        try:
            txt = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        d = clip_diagram(txt, max_lines=max_lines, max_width=max_width)
        if d.strip() and sha256_text(d) != current_hash:
            return d, str(fp)
    return None, None


def find_swap_diagram(
    problem_id: str,
    save_dir: Optional[Path],
    current_hash: str,
    *,
    swap_mode: str = "auto",
    swap_bank_path: Optional[str] = None,
    max_lines: int = 16,
    max_width: int = 64,
    seed: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if swap_mode not in ("auto", "bank_only", "saved_only"):
        raise ValueError(f"Unknown swap_mode: {swap_mode}")

    if swap_mode in ("auto", "bank_only"):
        diagram, source = find_swap_diagram_from_bank(
            problem_id,
            current_hash,
            swap_bank_path=swap_bank_path,
            max_lines=max_lines,
            max_width=max_width,
            seed=seed,
        )
        if diagram is not None and source is not None:
            return diagram, source
        if swap_mode == "bank_only":
            return None, None

    return find_saved_swap_diagram(save_dir, current_hash, max_lines=max_lines, max_width=max_width)


def _rotated_candidates(items: List[Any], seed: Optional[int]) -> List[Any]:
    if len(items) <= 1:
        return list(items)
    if seed is None:
        return list(items)
    start = int(seed) % len(items)
    return list(items[start:] + items[:start])


EQUIV_DIAGRAM_FLIP_TRANSLATION = str.maketrans({
    "^": "v",
    "v": "^",
    "/": "\\",
    "\\": "/",
})

EQUIV_DIAGRAM_REMAP_TRANSLATION = str.maketrans({
    "*": "#",
    "#": "*",
    ":": "~",
    "~": ":",
    ".": "+",
    "+": ".",
})


def transform_equivalent_diagram(diagram: str, *, mode: str = "vertical_flip_remap") -> str:
    if mode not in EQUIV_DIAGRAM_MODES:
        raise ValueError(f"Unknown equiv diagram mode: {mode}. Use one of {EQUIV_DIAGRAM_MODES}")

    s = (diagram or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    if not s:
        return ""

    out = s
    if mode in ("vertical_flip", "vertical_flip_remap"):
        lines = out.splitlines()
        out = "\n".join(ln.translate(EQUIV_DIAGRAM_FLIP_TRANSLATION) for ln in reversed(lines))

    if mode in ("remap_only", "vertical_flip_remap"):
        out = out.translate(EQUIV_DIAGRAM_REMAP_TRANSLATION)

    return out.rstrip()


def find_cross_diagram_from_bank(
    current_problem_id: str,
    current_hash: str,
    *,
    preferred_problem_id: Optional[str] = None,
    swap_bank_path: Optional[str] = None,
    max_lines: int = 16,
    max_width: int = 64,
    seed: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    bank = load_swap_bank(swap_bank_path, max_lines=max_lines, max_width=max_width)
    if not bank:
        return None, None, None

    problem_ids = [pid for pid in bank.keys() if pid != current_problem_id]
    if preferred_problem_id:
        if preferred_problem_id == current_problem_id:
            return None, None, None
        if preferred_problem_id in problem_ids:
            problem_ids = [preferred_problem_id] + [pid for pid in problem_ids if pid != preferred_problem_id]
        else:
            return None, None, None
    elif seed is not None:
        problem_ids = _rotated_candidates(sorted(problem_ids), seed)
    else:
        problem_ids = sorted(problem_ids)

    for pid in problem_ids:
        candidates = list(bank.get(pid, []))
        if seed is not None:
            candidates = _rotated_candidates(candidates, seed)
        for item in candidates:
            diagram = item["diagram"]
            if diagram.strip() and sha256_text(diagram) != current_hash:
                return diagram, f"bank:{pid}:{item['id']}", pid

    return None, None, None


def find_saved_cross_diagram(
    current_problem_id: str,
    save_dir: Optional[Path],
    current_hash: str,
    *,
    preferred_problem_id: Optional[str] = None,
    max_lines: int = 16,
    max_width: int = 64,
    seed: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not save_dir or not save_dir.exists():
        return None, None, None

    candidates: List[Tuple[str, str, str]] = []
    for json_fp in sorted(save_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(json_fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        problem_id = str(data.get("problem_id", "")).strip()
        if not problem_id or problem_id == current_problem_id:
            continue
        if preferred_problem_id and problem_id != preferred_problem_id:
            continue

        diag_fp = json_fp.with_suffix(".diagram.txt")
        if not diag_fp.exists():
            continue
        try:
            diagram_raw = diag_fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        diagram = clip_diagram(diagram_raw, max_lines=max_lines, max_width=max_width)
        if not diagram.strip() or sha256_text(diagram) == current_hash:
            continue
        candidates.append((problem_id, str(diag_fp), diagram))

    if preferred_problem_id and not candidates:
        return None, None, None

    if seed is not None:
        candidates = _rotated_candidates(candidates, seed)

    if not candidates:
        return None, None, None

    pid, src, diagram = candidates[0]
    return diagram, src, pid


def find_cross_diagram(
    current_problem_id: str,
    save_dir: Optional[Path],
    current_hash: str,
    *,
    source_mode: str = "auto",
    preferred_problem_id: Optional[str] = None,
    swap_bank_path: Optional[str] = None,
    max_lines: int = 16,
    max_width: int = 64,
    seed: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if source_mode not in ("auto", "bank_only", "saved_only"):
        raise ValueError(f"Unknown cross source mode: {source_mode}")

    if source_mode in ("auto", "bank_only"):
        diagram, source, source_problem_id = find_cross_diagram_from_bank(
            current_problem_id,
            current_hash,
            preferred_problem_id=preferred_problem_id,
            swap_bank_path=swap_bank_path,
            max_lines=max_lines,
            max_width=max_width,
            seed=seed,
        )
        if diagram is not None and source is not None and source_problem_id is not None:
            return diagram, source, source_problem_id
        if source_mode == "bank_only":
            return None, None, None

    return find_saved_cross_diagram(
        current_problem_id,
        save_dir,
        current_hash,
        preferred_problem_id=preferred_problem_id,
        max_lines=max_lines,
        max_width=max_width,
        seed=seed,
    )


# ======================
# 4) LLM クライアント抽象 + Provider実装
# ======================

class BaseLLMClient:
    def __init__(self, model: str, temperature: float, max_output_tokens: int, timeout: Optional[float] = None):
        self.model = model
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self.timeout = timeout

    @staticmethod
    def split_system(messages: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, str]]]:
        system_parts: List[str] = []
        convo: List[Dict[str, str]] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            if role == "system":
                if content.strip():
                    system_parts.append(content)
            else:
                convo.append({"role": role, "content": content})
        return "\n\n".join(system_parts).strip(), convo

    def chat(self, messages: List[Dict[str, str]]) -> str:
        raise NotImplementedError


class OpenAIClient(BaseLLMClient):
    """
    OpenAI Responses API:
      - system相当は `instructions`
      - 本文は `input`（array of role/content）
    """
    def __init__(self, model: str, temperature: float, max_output_tokens: int,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 reasoning_effort: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=timeout)
        try:
            from openai import OpenAI, BadRequestError
        except ImportError as e:
            raise RuntimeError("OpenAI provider requires `pip install openai`.") from e

        kwargs: Dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.reasoning_effort = reasoning_effort
        self.bad_request_error_cls = BadRequestError
        self._temperature_supported: Optional[bool] = None

    def _should_retry_without_temperature(self, exc: Exception) -> bool:
        msg = str(exc)
        return (
            "Unsupported parameter" in msg
            and "temperature" in msg
            and "not supported with this model" in msg
        )

    def _extract_output_text(self, resp: Any) -> str:
        txt = getattr(resp, "output_text", "")
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
        try:
            dump = resp.model_dump()
        except Exception:
            return ""
        outputs = dump.get("output", []) if isinstance(dump, dict) else []
        parts: List[str] = []
        for item in outputs:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    t = content.get("text", "")
                    if isinstance(t, str) and t:
                        parts.append(t)
        return "".join(parts).strip()

    def _is_empty_incomplete_max_output(self, resp: Any, text: str) -> bool:
        if text.strip():
            return False
        status = getattr(resp, "status", None)
        incomplete = getattr(resp, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None) if incomplete is not None else None
        if reason is None and isinstance(incomplete, dict):
            reason = incomplete.get("reason")
        return status == "incomplete" and reason == "max_output_tokens"

    def _is_reasoning_model(self) -> bool:
        return self.model.startswith("gpt-5") or self.model.startswith("o")

    def chat(self, messages: List[Dict[str, str]]) -> str:
        system, convo = self.split_system(messages)
        base_payload: Dict[str, Any] = {
            "model": self.model,
            "input": convo,
            "max_output_tokens": self.max_output_tokens,
        }
        if system:
            base_payload["instructions"] = system
        if self.reasoning_effort:
            base_payload["reasoning"] = {"effort": self.reasoning_effort}

        def send(payload: Dict[str, Any]) -> Any:
            try:
                resp = self.client.responses.create(**payload)
                if "temperature" in payload:
                    self._temperature_supported = True
                return resp
            except self.bad_request_error_cls as e:
                if "temperature" not in payload or not self._should_retry_without_temperature(e):
                    raise
                self._temperature_supported = False
                payload2 = dict(payload)
                payload2.pop("temperature", None)
                return self.client.responses.create(**payload2)

        payload = dict(base_payload)
        if self._temperature_supported is not False:
            payload["temperature"] = self.temperature
        resp = send(payload)
        text = self._extract_output_text(resp)
        if self._is_empty_incomplete_max_output(resp, text):
            if self.reasoning_effort is None and self._is_reasoning_model():
                retry_payload = dict(base_payload)
                retry_payload["max_output_tokens"] = max(int(self.max_output_tokens), 2000)
                retry_payload["reasoning"] = {"effort": "low"}
                if self._temperature_supported is not False:
                    retry_payload["temperature"] = self.temperature
                resp = send(retry_payload)
                text = self._extract_output_text(resp)
            if self._is_empty_incomplete_max_output(resp, text):
                raise RuntimeError(
                    f"OpenAI response incomplete with empty text (model={self.model}, "
                    f"max_output_tokens={getattr(resp, 'max_output_tokens', self.max_output_tokens)})."
                )
        return text


class AnthropicClient(BaseLLMClient):
    """
    Anthropic Messages API:
      - systemはトップレベル `system`
    """
    def __init__(self, model: str, temperature: float, max_output_tokens: int,
                 api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=timeout)
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("Anthropic provider requires `pip install anthropic`.") from e

        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def chat(self, messages: List[Dict[str, str]]) -> str:
        system, convo = self.split_system(messages)

        anthro_msgs: List[Dict[str, Any]] = []
        for m in convo:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                role = "user"
            anthro_msgs.append({"role": role, "content": m.get("content", "")})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": anthro_msgs,
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }
        if system:
            kwargs["system"] = system

        resp = self.client.messages.create(**kwargs)

        text_parts: List[str] = []
        for blk in getattr(resp, "content", []):
            btype = getattr(blk, "type", None) if not isinstance(blk, dict) else blk.get("type")
            if btype == "text":
                text_parts.append(getattr(blk, "text", "") if not isinstance(blk, dict) else blk.get("text", ""))
        return "".join(text_parts).strip()


class MistralClient(BaseLLMClient):
    def __init__(self, model: str, temperature: float, max_output_tokens: int,
                 api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=timeout)
        try:
            from mistralai import Mistral
        except ImportError as e:
            raise RuntimeError("Mistral provider requires `pip install mistralai`.") from e

        self.client = Mistral(api_key=api_key or os.getenv("MISTRAL_API_KEY", ""))

    def chat(self, messages: List[Dict[str, str]]) -> str:
        msgs: List[Dict[str, str]] = []
        for m in messages:
            role = m.get("role", "")
            if role not in ("system", "user", "assistant"):
                role = "user"
            msgs.append({"role": role, "content": m.get("content", "")})

        res = self.client.chat.complete(
            model=self.model,
            messages=msgs,
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
            stream=False,
        )
        try:
            return (res.choices[0].message.content or "").strip()
        except Exception:
            return str(res).strip()


class GoogleGenAIClient(BaseLLMClient):
    """
    Google GenAI SDK (google-genai):
      - client = genai.Client()
      - client.models.generate_content(...)
    """
    def __init__(self, model: str, temperature: float, max_output_tokens: int,
                 api_key: Optional[str] = None,
                 timeout: Optional[float] = None,
                 retry_429: int = 2,
                 debug_empty: bool = True):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=timeout)
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise RuntimeError("Google provider requires `pip install google-genai`.") from e

        self.genai = genai
        self.types = types
        self.retry_429 = int(retry_429)
        self.debug_empty = bool(debug_empty)

        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def _get(self, obj: Any, attr: str, default: Any = None) -> Any:
        try:
            return getattr(obj, attr)
        except Exception:
            return default

    def _to_plain(self, obj: Any, depth: int = 0, max_depth: int = 6) -> Any:
        """
        resp を dict/list/primitive に落とす（debug用）
        """
        if depth > max_depth:
            return repr(obj)

        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {str(k): self._to_plain(v, depth + 1, max_depth) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._to_plain(v, depth + 1, max_depth) for v in obj]

        # pydantic / dataclass / custom
        if hasattr(obj, "model_dump"):
            try:
                return self._to_plain(obj.model_dump(), depth + 1, max_depth)
            except Exception:
                pass
        if hasattr(obj, "__dict__"):
            try:
                d = {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
                return self._to_plain(d, depth + 1, max_depth)
            except Exception:
                pass

        return repr(obj)

    def _extract_text_robust(self, resp: Any) -> str:
        """
        本文抽出は candidates.content.parts[].text（+ resp.text）に限定する。
        それ以外（ヘッダ/メタ文字列）を拾わない。
        """
        # 1) resp.text
        t = self._get(resp, "text", None)
        if isinstance(t, str) and t.strip():
            return t.strip()

        # 2) candidates.parts.text
        d = self._to_plain(resp)
        if not isinstance(d, dict):
            return ""

        texts: List[str] = []
        cands = d.get("candidates", []) or []
        if isinstance(cands, list):
            for c in cands:
                if not isinstance(c, dict):
                    continue
                content = c.get("content") or c.get("message") or {}
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts") or []
                if not isinstance(parts, list):
                    continue
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        texts.append(p.get("text", ""))

        out = "".join(texts)
        return out.strip()

    def _debug_dump(self, resp: Any, *, label: str, input_sample: Optional[str] = None):
        if not self.debug_empty:
            return
        d = self._to_plain(resp)
        try:
            cands = d.get("candidates", []) if isinstance(d, dict) else []
            print(f"[GoogleGenAIClient] EMPTY ({label}) candidates={len(cands) if isinstance(cands, list) else 'n/a'}")
            if isinstance(cands, list):
                for i, c in enumerate(cands[:2]):
                    if not isinstance(c, dict):
                        continue
                    fr = c.get("finish_reason")
                    print(f"  cand[{i}].finish_reason={fr}")
                    content = c.get("content") or {}
                    parts = content.get("parts") if isinstance(content, dict) else None
                    if isinstance(parts, list) and parts:
                        keys = [sorted(list(p.keys())) for p in parts if isinstance(p, dict)]
                        print(f"  cand[{i}].parts_keys={keys[:3]}")
            pf = d.get("prompt_feedback") if isinstance(d, dict) else None
            print(f"  prompt_feedback={pf}")
            if input_sample:
                # 長すぎると邪魔なので先頭だけ
                print("  input_sample(struct)=", input_sample[:450].replace("\n", "\\n"))
        except Exception:
            print("[GoogleGenAIClient] EMPTY (debug_dump failed)")

    def chat(self, messages: List[Dict[str, str]]) -> str:
        system, convo = self.split_system(messages)

        # stateless: 会話ログをテキスト化（役割ラベルつき）
        lines = []
        for m in convo:
            role = m.get("role", "user")
            prefix = "User" if role == "user" else "Assistant" if role == "assistant" else role
            lines.append(f"{prefix}: {m.get('content','')}")
        contents = "\n\n".join(lines).strip()

        cfg_kwargs: Dict[str, Any] = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        if system:
            cfg_kwargs["system_instruction"] = system

        config = self.types.GenerateContentConfig(**cfg_kwargs)

        last_exc: Optional[Exception] = None
        for attempt in range(self.retry_429 + 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                text = self._extract_text_robust(resp)
                if text.strip() == "":
                    self._debug_dump(resp, label="generate_content", input_sample=contents)
                return text
            except Exception as e:
                last_exc = e
                msg = str(e)
                if ("429" in msg) or ("RESOURCE_EXHAUSTED" in msg):
                    if attempt >= self.retry_429:
                        raise
                    m = re.search(r"retry in\s+([0-9]+(\.[0-9]+)?)s", msg, re.IGNORECASE)
                    delay = float(m.group(1)) if m else 20.0
                    time.sleep(max(0.0, delay))
                    continue
                raise
        if last_exc:
            raise last_exc
        return ""


class HFLocalClient(BaseLLMClient):
    """
    ローカルHFモデル（transformers）:
      - offline/local_files_only 対応
      - device_map auto の disk offload 対応
      - chat_template の role交互制約回避（system吸収＋連続role結合＋fallback transcript）
    """
    def __init__(
        self,
        model: str,
        temperature: float,
        max_output_tokens: int,
        device: str = "auto",
        dtype: str = "auto",
        trust_remote_code: bool = False,
        load_in_8bit: bool = False,
        load_in_4bit: bool = False,
        offline: bool = False,
        local_files_only: bool = False,
        cache_dir: Optional[str] = None,
        offload_folder: str = "offload",
        disable_chat_template: bool = False,
    ):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=None)

        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM
        except ImportError as e:
            raise RuntimeError("HF provider requires `pip install transformers torch accelerate`.") from e

        self.torch = torch
        self.disable_chat_template = bool(disable_chat_template)

        if offline:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            local_files_only = True

        p = Path(model).expanduser()
        if p.exists():
            model_id_or_path = str(p.resolve())
        else:
            if local_files_only:
                raise FileNotFoundError(
                    f"HF offline/local_files_only=True ですが、ローカルモデルパスが見つかりません: {p}\n"
                    f"（repo idとして解釈せず即停止します）"
                )
            model_id_or_path = model

        common_kwargs: Dict[str, Any] = {
            "trust_remote_code": trust_remote_code,
            "local_files_only": local_files_only,
        }
        if cache_dir:
            common_kwargs["cache_dir"] = cache_dir

        self.tokenizer = AutoTokenizer.from_pretrained(model_id_or_path, **common_kwargs)

        model_kwargs: Dict[str, Any] = {"trust_remote_code": trust_remote_code}
        if load_in_8bit:
            model_kwargs["load_in_8bit"] = True
        if load_in_4bit:
            model_kwargs["load_in_4bit"] = True

        dtype_obj = None
        if dtype != "auto":
            if not hasattr(torch, dtype):
                raise ValueError(f"Unknown dtype: {dtype} (expected float16/bfloat16/float32/auto)")
            dtype_obj = getattr(torch, dtype)

        if device == "auto":
            model_kwargs["device_map"] = "auto"
            if offload_folder:
                Path(offload_folder).mkdir(parents=True, exist_ok=True)
                model_kwargs["offload_folder"] = offload_folder

        # ロード（dtypeは互換フォールバック）
        if dtype_obj is not None:
            try:
                self.model_obj = AutoModelForCausalLM.from_pretrained(
                    model_id_or_path,
                    dtype=dtype_obj,
                    **common_kwargs,
                    **model_kwargs,
                )
            except TypeError:
                self.model_obj = AutoModelForCausalLM.from_pretrained(
                    model_id_or_path,
                    torch_dtype=dtype_obj,
                    **common_kwargs,
                    **model_kwargs,
                )
        else:
            self.model_obj = AutoModelForCausalLM.from_pretrained(
                model_id_or_path,
                **common_kwargs,
                **model_kwargs,
            )

        if device != "auto":
            self.model_obj.to(device)

        self.model_obj.eval()
        self.field_trace_config: Optional[Dict[str, Any]] = None
        self.last_field_metrics: Optional[FieldMetrics] = None
        self.last_generation: Optional[Dict[str, Any]] = None

    def chat(self, messages: List[Dict[str, str]]) -> str:
        self.last_field_metrics = None
        self.last_generation = None

        system, convo = self.split_system(messages)

        # 1) user/assistant のみに正規化
        norm: List[Dict[str, str]] = []
        for m in convo:
            role = m.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            norm.append({"role": role, "content": str(m.get("content", ""))})

        # 2) 同じroleが連続したら結合（交互制約回避）
        merged: List[Dict[str, str]] = []
        for m in norm:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(m)

        # 3) system は先頭 user に吸収（system role 非対応テンプレ対策）
        if system:
            if merged and merged[0]["role"] == "user":
                merged[0]["content"] = system + "\n\n" + merged[0]["content"]
            else:
                merged.insert(0, {"role": "user", "content": system})

        # 4) chat templateを試し、ダメなら手動transcript
        prompt = None
        if (not self.disable_chat_template) and hasattr(self.tokenizer, "apply_chat_template") and getattr(self.tokenizer, "chat_template", None):
            try:
                prompt = self.tokenizer.apply_chat_template(
                    merged,
                    tokenize=False,
                    add_generation_prompt=True,
                )
            except Exception:
                prompt = None

        if prompt is None:
            prompt = ""
            for m in merged:
                if m["role"] == "user":
                    prompt += f"User: {m['content']}\n"
                else:
                    prompt += f"Assistant: {m['content']}\n"
            prompt += "Assistant: "

        inputs = self.tokenizer(prompt, return_tensors="pt")
        inputs = {k: v.to(self.model_obj.device) for k, v in inputs.items()}

        pad_id = self.tokenizer.pad_token_id
        if pad_id is None:
            pad_id = self.tokenizer.eos_token_id

        gen_kwargs: Dict[str, Any] = {"max_new_tokens": self.max_output_tokens, "pad_token_id": pad_id}
        if self.temperature and self.temperature > 0:
            gen_kwargs.update({"do_sample": True, "temperature": float(self.temperature)})
        else:
            gen_kwargs.update({"do_sample": False})

        cfg = getattr(self, "field_trace_config", None)
        if cfg:
            text, fm = self._generate_with_field_metrics(inputs=inputs, gen_kwargs=gen_kwargs, cfg=cfg)
            self.last_field_metrics = fm
            return text

        out = self.model_obj.generate(**inputs, **gen_kwargs)
        prompt_len = int(inputs["input_ids"].shape[-1])
        self.last_generation = {
            "prompt_len": prompt_len,
            "sequences": out[0].detach(),
        }
        new_tokens = out[0][prompt_len:]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

    def _parse_layer_index(self, spec: str, n_layers: int) -> int:
        s = (spec or "").strip().lower()
        if s in ("last", "-1"):
            return n_layers - 1
        try:
            i = int(s)
        except Exception as e:
            raise ValueError(f"Invalid layer index: {spec!r}") from e
        if i < 0:
            i = n_layers + i
        if i < 0 or i >= n_layers:
            raise ValueError(f"Layer index out of range: {spec!r} (n_layers={n_layers})")
        return i

    def _parse_layers_spec(self, spec: str, n_layers: int) -> List[int]:
        s = (spec or "").strip().lower()
        if s in ("", "all"):
            return list(range(n_layers))
        if s in ("last",):
            return [n_layers - 1]
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if not parts:
            return list(range(n_layers))
        out: List[int] = []
        for p in parts:
            if p.lower() == "last":
                out.append(n_layers - 1)
            else:
                out.append(self._parse_layer_index(p, n_layers))
        # de-dup while preserving order
        seen = set()
        uniq: List[int] = []
        for i in out:
            if i not in seen:
                uniq.append(i)
                seen.add(i)
        return uniq

    def _powerlaw_fit_alpha(self, eigvals_desc: List[float], *, fit_k: int) -> Tuple[float, float]:
        """
        Fit eig[k] ~ k^{-alpha} on log-log for the top-k positive eigenvalues.
        Returns (alpha, r2). If fit isn't possible, returns (nan, nan).
        """
        vals = [v for v in eigvals_desc if v > 0.0]
        if not vals:
            return float("nan"), float("nan")
        k = min(int(fit_k), len(vals))
        if k < 3:
            return float("nan"), float("nan")
        xs = [math.log(i) for i in range(1, k + 1)]
        ys = [math.log(float(vals[i - 1])) for i in range(1, k + 1)]
        x_mean = sum(xs) / k
        y_mean = sum(ys) / k
        var_x = sum((x - x_mean) ** 2 for x in xs)
        if var_x <= 0.0:
            return float("nan"), float("nan")
        cov_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        slope = cov_xy / var_x
        intercept = y_mean - slope * x_mean
        ss_tot = sum((y - y_mean) ** 2 for y in ys)
        ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0.0 else float("nan")
        alpha = -slope
        return float(alpha), float(r2)

    def _compute_field_stats(self, vecs: List["Any"], *, fit_k: int) -> Tuple[float, float, float, float, float]:
        """
        vecs: list of 1D tensors (hidden_dim).
        Returns: (dim_eff, anisotropy, content_mass, curvature_alpha, curvature_r2)
        """
        torch = self.torch
        n = len(vecs)
        if n < 2:
            return 0.0, 0.0, 0.0, float("nan"), float("nan")

        X = torch.stack(vecs, dim=0).float()
        X = X - X.mean(dim=0, keepdim=True)
        denom = float(max(1, n - 1))
        G = (X @ X.t()) / denom

        try:
            eig = torch.linalg.eigvalsh(G.detach().cpu())
        except Exception:
            # fallback for older torch
            try:
                eig = torch.symeig(G.detach().cpu(), eigenvectors=False).eigenvalues  # type: ignore[attr-defined]
            except Exception:
                return 0.0, 0.0, 0.0, float("nan"), float("nan")

        eig = torch.clamp(eig, min=0.0)
        eig_desc = torch.flip(eig, dims=(0,))
        eig_list = [float(x) for x in eig_desc.tolist()]

        s1 = float(sum(eig_list))
        s2 = float(sum(v * v for v in eig_list))
        if s1 <= 0.0 or s2 <= 0.0:
            return 0.0, 0.0, 0.0, float("nan"), float("nan")

        dim_eff = (s1 * s1) / s2
        anisotropy = float(eig_list[0] / s1) if eig_list else 0.0
        content_mass = s1
        alpha, r2 = self._powerlaw_fit_alpha(eig_list, fit_k=int(fit_k))
        return float(dim_eff), float(anisotropy), float(content_mass), float(alpha), float(r2)

    def _find_valleys(self, points: List["FieldTimePoint"], metric: str) -> List[int]:
        vals: List[float] = []
        ts: List[int] = []
        for p in points:
            ts.append(int(p.t))
            vals.append(float(getattr(p, metric)))
        out: List[int] = []
        for i in range(1, len(vals) - 1):
            d0 = vals[i] - vals[i - 1]
            d1 = vals[i + 1] - vals[i]
            if d0 < 0.0 and d1 > 0.0:
                out.append(ts[i])
        return out

    def _generate_with_field_metrics(
        self,
        *,
        inputs: Dict[str, Any],
        gen_kwargs: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> Tuple[str, "FieldMetrics"]:
        torch = self.torch

        window = int(cfg.get("window", 128))
        time_every = max(1, int(cfg.get("time_every", 5)))
        fit_k = max(4, int(cfg.get("fit_k", 64)))
        time_layer_spec = str(cfg.get("time_layer", "last"))
        layers_spec = str(cfg.get("layers", "all"))

        input_ids = inputs["input_ids"]
        attention_mask = inputs.get("attention_mask", torch.ones_like(input_ids))
        prompt_len = int(input_ids.shape[-1])
        max_new_tokens = int(gen_kwargs.get("max_new_tokens", self.max_output_tokens))
        do_sample = bool(gen_kwargs.get("do_sample", False))
        temperature = float(gen_kwargs.get("temperature", self.temperature))

        eos_id = self.tokenizer.eos_token_id
        if eos_id is None:
            eos_id = getattr(getattr(self.model_obj, "config", None), "eos_token_id", None)
        eos_ids: List[int] = []
        if isinstance(eos_id, int):
            eos_ids = [int(eos_id)]
        elif isinstance(eos_id, list):
            eos_ids = [int(x) for x in eos_id if isinstance(x, int)]

        def sample_next(logits_1d: "Any") -> int:
            if (not do_sample) or (temperature is None) or (temperature <= 0.0):
                return int(torch.argmax(logits_1d).item())
            probs = torch.softmax(logits_1d / float(temperature), dim=-1)
            idx = torch.multinomial(probs, num_samples=1)
            return int(idx.item())

        with torch.no_grad():
            if max_new_tokens < 1:
                raise ValueError(f"max_new_tokens must be >= 1 (got {max_new_tokens})")

            # Step 0: run prompt to prime KV cache and get the logits for the first token
            out0 = self.model_obj(
                input_ids=input_ids,
                attention_mask=attention_mask,
                use_cache=True,
                output_hidden_states=True,
                return_dict=True,
            )
            past = out0.past_key_values
            logits_next = out0.logits[0, -1, :]

            num_layers = len(getattr(out0, "hidden_states", []) or [])
            if num_layers <= 0:
                raise RuntimeError("Hidden states not available (model/config does not return hidden_states).")

            layers_to_track = self._parse_layers_spec(layers_spec, num_layers)
            time_layer = self._parse_layer_index(time_layer_spec, num_layers)

            from collections import deque
            bufs: Dict[int, "Any"] = {}
            for li in layers_to_track:
                bufs[li] = deque(maxlen=(window if window > 0 else None))

            generated_ids: List[int] = []
            processed_ids: List[int] = []
            time_points: List[FieldTimePoint] = []

            attention_mask_full = attention_mask

            def collect_hidden_states(hs_tuple: Any, token_id: int):
                # hs_tuple: tuple[layer] -> tensor(1, seq, hidden)
                for li in layers_to_track:
                    hs = hs_tuple[li]
                    vec = hs[0, -1, :].detach().to(device="cpu", dtype=torch.float32)
                    bufs[li].append(vec)
                processed_ids.append(int(token_id))

            def maybe_record_time_point(t: int):
                if time_layer not in bufs:
                    return
                vecs = list(bufs[time_layer])
                dim_eff, anis, mass, alpha, r2 = self._compute_field_stats(vecs, fit_k=fit_k)
                time_points.append(FieldTimePoint(
                    t=int(t),
                    window_n=len(vecs),
                    dim_eff=float(dim_eff),
                    anisotropy=float(anis),
                    content_mass=float(mass),
                    curvature_alpha=float(alpha),
                    curvature_r2=float(r2),
                ))

            # Generate and process tokens 1..N
            for _ in range(max_new_tokens):
                token_id = sample_next(logits_next)
                generated_ids.append(int(token_id))

                cur = torch.tensor([[int(token_id)]], device=input_ids.device)
                attention_mask_full = torch.cat(
                    [attention_mask_full, torch.ones((1, 1), dtype=attention_mask_full.dtype, device=attention_mask_full.device)],
                    dim=1,
                )
                out = self.model_obj(
                    input_ids=cur,
                    attention_mask=attention_mask_full,
                    past_key_values=past,
                    use_cache=True,
                    output_hidden_states=True,
                    return_dict=True,
                )
                past = out.past_key_values
                hs = getattr(out, "hidden_states", None)
                if hs is None:
                    raise RuntimeError("Hidden states missing during generation.")
                collect_hidden_states(hs, token_id=int(token_id))

                t = len(processed_ids)
                if (t % time_every) == 0:
                    maybe_record_time_point(t)

                logits_next = out.logits[0, -1, :]

                if eos_ids and int(token_id) in eos_ids:
                    break

            # Ensure final time point is recorded
            if processed_ids:
                t_final = len(processed_ids)
                if (not time_points) or (time_points[-1].t != t_final):
                    maybe_record_time_point(t_final)

            # Layer-wise trajectory (final window for each tracked layer)
            layer_metrics: List[FieldLayerMetrics] = []
            for li in layers_to_track:
                vecs = list(bufs.get(li, []))
                dim_eff, anis, mass, alpha, r2 = self._compute_field_stats(vecs, fit_k=fit_k)
                layer_metrics.append(FieldLayerMetrics(
                    layer=int(li),
                    n_tokens=len(vecs),
                    dim_eff=float(dim_eff),
                    anisotropy=float(anis),
                    content_mass=float(mass),
                    curvature_alpha=float(alpha),
                    curvature_r2=float(r2),
                ))

            # Detect shrink->expand events (valleys) on selected metrics
            events: List[FieldEvent] = []
            for metric in ("dim_eff", "content_mass"):
                for t in self._find_valleys(time_points, metric=metric):
                    tok_id = processed_ids[t - 1] if 0 <= (t - 1) < len(processed_ids) else None
                    tok = ""
                    tail = ""
                    if tok_id is not None:
                        try:
                            tok = self.tokenizer.convert_ids_to_tokens([int(tok_id)])[0]
                        except Exception:
                            tok = str(tok_id)
                        try:
                            prefix = self.tokenizer.decode(processed_ids[:t], skip_special_tokens=True)
                            tail = prefix[-220:]
                        except Exception:
                            tail = ""
                    events.append(FieldEvent(metric=str(metric), t=int(t), token=tok, text_tail=tail))

            seq_len = prompt_len + len(processed_ids)
            fm = FieldMetrics(
                scope="generated",
                window=int(window),
                time_layer=int(time_layer),
                time_every=int(time_every),
                fit_k=int(fit_k),
                prompt_len=int(prompt_len),
                seq_len=int(seq_len),
                scope_token_start=int(prompt_len),
                scope_token_end=int(prompt_len + len(processed_ids)),
                layers=layer_metrics,
                time=time_points,
                events=events,
            )

            # For compatibility / debugging
            self.last_generation = {
                "prompt_len": prompt_len,
                "generated_ids": list(generated_ids),
                "processed_ids": list(processed_ids),
            }

            text = self.tokenizer.decode(processed_ids, skip_special_tokens=True).strip()
            return text, fm


def make_llm(args) -> BaseLLMClient:
    provider = args.provider
    model = args.model
    temp = args.temperature
    max_out = args.max_output_tokens

    if provider == "openai":
        return OpenAIClient(
            model=model, temperature=temp, max_output_tokens=max_out,
            api_key=args.api_key,
            base_url=args.openai_base_url,
            reasoning_effort=args.openai_reasoning_effort,
        )
    if provider == "anthropic":
        return AnthropicClient(
            model=model, temperature=temp, max_output_tokens=max_out,
            api_key=args.api_key,
        )
    if provider == "mistral":
        return MistralClient(
            model=model, temperature=temp, max_output_tokens=max_out,
            api_key=args.api_key,
        )
    if provider == "google":
        return GoogleGenAIClient(
            model=model, temperature=temp, max_output_tokens=max_out,
            api_key=args.api_key,
            retry_429=args.google_retry_429,
            debug_empty=args.google_debug_empty,
        )
    if provider == "hf":
        return HFLocalClient(
            model=model, temperature=temp, max_output_tokens=max_out,
            device=args.hf_device,
            dtype=args.hf_dtype,
            trust_remote_code=args.hf_trust_remote_code,
            load_in_8bit=args.hf_load_in_8bit,
            load_in_4bit=args.hf_load_in_4bit,
            offline=args.offline,
            local_files_only=args.hf_local_files_only,
            cache_dir=args.hf_cache_dir,
            offload_folder=args.hf_offload_folder,
            disable_chat_template=args.hf_disable_chat_template,
        )

    raise ValueError(f"Unknown provider: {provider}")


# ======================
# 5) Phase A/B/C
# ======================

@dataclass
class PhaseAResult:
    seed: str
    diagram: str
    tags: List[str]
    unknown_tags: List[str]
    diagram_hash: str
    used_fallback_tags: bool = False

@dataclass
class Contribution2x2:
    enabled: bool
    no_diagram_answer: str = ""
    no_diagram_similarity: float = -1.0
    no_tags_answer: str = ""
    no_tags_similarity: float = -1.0
    neither_answer: str = ""
    neither_similarity: float = -1.0

@dataclass
class DiagramTests:
    enabled: bool
    corruption_mode: str = "noise"
    corruption_rate: float = 0.12
    corrupt_diagram_hash: str = ""
    corrupt_answer: str = ""
    corrupt_similarity: float = -1.0

    swap_used: bool = False
    swap_source: str = ""
    swap_source_kind: str = ""
    swap_diagram_hash: str = ""
    swap_answer: str = ""
    swap_similarity: float = -1.0

@dataclass
class TestResult:
    test_mode: str
    temperature_test: float
    base_answer_test: str

    contrib: Contribution2x2
    diagram_tests: DiagramTests

    tamper_remove_used: Optional[str]
    tamper_add_used: Optional[str]
    tamper_remove_answer: str
    tamper_remove_similarity: float
    tamper_add_answer: str
    tamper_add_similarity: float
    tamper_both_answer: str
    tamper_both_similarity: float

@dataclass
class ConditionMatrixEntry:
    condition_id: str
    label: str
    query_mode: str
    diagram_mode: str
    tags_mode: str
    diagram_hash: str
    answer: str
    similarity: float
    status: str
    compare_mode: str = "surface"
    axis_guide_mode: str = ""
    surface_similarity: float = -1.0
    semantic_similarity: float = -1.0
    comparison_label: str = ""
    comparison_reason: str = ""
    axis_adherence_label: str = ""
    axis_adherence_score: float = -1.0
    axis_adherence_axes: str = ""
    axis_adherence_reason: str = ""
    diagram_source: str = ""
    diagram_source_kind: str = ""
    source_problem_id: str = ""
    notes: str = ""

@dataclass
class ConditionMatrixJudgment:
    pass_threshold: float
    soft_threshold: float
    no_query_strict_status: str = CONDITION_STATUS_SKIPPED
    no_query_with_axis_status: str = CONDITION_STATUS_SKIPPED
    no_query_status: str = CONDITION_STATUS_SKIPPED
    equiv_diagram_status: str = CONDITION_STATUS_SKIPPED
    cross_diagram_status: str = CONDITION_STATUS_SKIPPED
    overall_verdict: str = "mixed_or_inconclusive"
    overall_reason: str = ""
    axis_adherence_verdict: str = ""
    axis_adherence_reason: str = ""

@dataclass
class ConditionMatrixResult:
    enabled: bool
    temperature_test: float
    requested_conditions: List[str]
    compare_mode: str
    equiv_diagram_mode: str
    cross_source_mode: str
    cross_problem_requested: str
    baseline_answer: str
    entries: List[ConditionMatrixEntry]
    judgment: Optional[ConditionMatrixJudgment] = None

@dataclass
class FieldLayerMetrics:
    layer: int
    n_tokens: int
    dim_eff: float
    anisotropy: float
    content_mass: float
    curvature_alpha: float
    curvature_r2: float

@dataclass
class FieldTimePoint:
    t: int                  # 1..N within the chosen scope
    window_n: int           # number of tokens used for this point (<= window if window>0)
    dim_eff: float
    anisotropy: float
    content_mass: float
    curvature_alpha: float
    curvature_r2: float

@dataclass
class FieldEvent:
    metric: str             # e.g. "dim_eff" or "content_mass"
    t: int                  # token index within scope (same axis as FieldTimePoint.t)
    token: str = ""         # token string (best-effort)
    text_tail: str = ""     # decoded tail up to t (best-effort)

@dataclass
class FieldMetrics:
    scope: str
    window: int
    time_layer: int
    time_every: int
    fit_k: int

    prompt_len: int
    seq_len: int
    scope_token_start: int  # 0-based index into the full sequence
    scope_token_end: int    # exclusive, 0-based

    layers: List[FieldLayerMetrics]
    time: List[FieldTimePoint]
    events: List[FieldEvent]

@dataclass
class RunResult:
    provider: str
    model: str
    problem_id: str
    query: str
    answer_mode: str
    prompt_priority: str
    run_seed: Optional[int]

    temperature_a: float
    temperature_answer: float
    temperature_test: float

    allow_tag_label_exception: bool
    phase_a_axis_binding: bool
    phase_a_axis_guide: List[str]
    phase_a_attempts: int
    phase_a_validation_errors: List[str]

    seed: str
    tags: List[str]
    unknown_tags: List[str]
    diagram_hash: str
    diagram_readback: str
    diagram_support: str

    answer: str
    caption_1line: str
    tests: Optional[TestResult] = None
    condition_matrix: Optional[ConditionMatrixResult] = None
    field_metrics: Optional[FieldMetrics] = None


def _diagram_block(diagram: str) -> str:
    """
    B/C で DIAGRAM を渡すためのブロック。
    code fence で囲う（改行・スペース保持のため）
    """
    d = (diagram or "").rstrip()
    return f"[DIAGRAM]\n```text\n{d}\n```\n\n"

def answer_mode_uses_tags(answer_mode: str) -> bool:
    if answer_mode not in ANSWER_MODES:
        raise ValueError(f"Unknown answer_mode: {answer_mode}. Use one of {ANSWER_MODES}")
    return answer_mode == "diagram_plus_tags"

def has_query_text(query: str) -> bool:
    return bool((query or "").strip())

def build_phase_a_prompt(query: str, prompt_priority: str, phase_a_axis_binding: bool = False) -> str:
    return PROMPT_A.format(
        query=query,
        vocab=vocab_hint(),
        allowed_symbols=PHASE_A_ALLOWED_SYMBOLS_DISPLAY,
        symbol_pool=PHASE_A_FREE_SYMBOL_POOL,
        phase_a_header=build_phase_a_header(prompt_priority),
        input_label=build_phase_a_input_label(prompt_priority),
        priority_preamble=_priority_preamble(prompt_priority, "a") + _axis_binding_preamble(prompt_priority, phase_a_axis_binding),
        axis_binding_constraints=_phase_a_axis_binding_constraint(prompt_priority, phase_a_axis_binding),
        axis_binding_guide_block=_axis_binding_guide_block(prompt_priority, phase_a_axis_binding, query),
    )


def build_phase_b_prompt(query: str, tags: List[str], answer_mode: str, prompt_priority: str) -> str:
    if not has_query_text(query):
        if answer_mode_uses_tags(answer_mode):
            return PROMPT_B_NO_QUERY_DIAGRAM_PLUS_TAGS.format(
                tags=", ".join(tags),
                priority_preamble=_priority_preamble(prompt_priority, "b"),
            )
        return PROMPT_B_NO_QUERY_DIAGRAM_ONLY.format(
            priority_preamble=_priority_preamble(prompt_priority, "b"),
        )
    if answer_mode_uses_tags(answer_mode):
        return PROMPT_B_DIAGRAM_PLUS_TAGS.format(
            query=query,
            tags=", ".join(tags),
            priority_preamble=_priority_preamble(prompt_priority, "b"),
        )
    return PROMPT_B_DIAGRAM_ONLY.format(
        query=query,
        priority_preamble=_priority_preamble(prompt_priority, "b"),
    )

def build_phase_b_readback_prompt(prompt_priority: str) -> str:
    return PROMPT_B_READBACK.format(
        priority_preamble=_priority_preamble(prompt_priority, "b"),
    )


def build_phase_b_align_prompt(query: str, readback: str, tags: List[str], answer_mode: str, prompt_priority: str) -> str:
    if not has_query_text(query):
        if answer_mode_uses_tags(answer_mode):
            return PROMPT_B_PROJECT_NO_QUERY_DIAGRAM_PLUS_TAGS.format(
                readback=readback,
                tags=", ".join(tags),
                priority_preamble=_priority_preamble(prompt_priority, "b"),
            )
        return PROMPT_B_PROJECT_NO_QUERY_DIAGRAM_ONLY.format(
            readback=readback,
            priority_preamble=_priority_preamble(prompt_priority, "b"),
        )
    if answer_mode_uses_tags(answer_mode):
        return PROMPT_B_ALIGN_DIAGRAM_PLUS_TAGS.format(
            query=query,
            readback=readback,
            tags=", ".join(tags),
            priority_preamble=_priority_preamble(prompt_priority, "b"),
        )
    return PROMPT_B_ALIGN_DIAGRAM_ONLY.format(
        query=query,
        readback=readback,
        priority_preamble=_priority_preamble(prompt_priority, "b"),
    )


def build_phase_b_support_prompt(
    query: str,
    readback: str,
    tags: List[str],
    answer_mode: str,
    prompt_priority: str,
    axis_guides: Optional[List[str]] = None,
) -> str:
    if not has_query_text(query):
        if answer_mode_uses_tags(answer_mode):
            return PROMPT_B_EXTRACT_SUPPORT_NO_QUERY_DIAGRAM_PLUS_TAGS.format(
                readback=readback,
                tags=", ".join(tags),
                priority_preamble=_priority_preamble(prompt_priority, "b"),
                axis_guide_block=_axis_guide_block_from_list(axis_guides),
            )
        return PROMPT_B_EXTRACT_SUPPORT_NO_QUERY_DIAGRAM_ONLY.format(
            readback=readback,
            priority_preamble=_priority_preamble(prompt_priority, "b"),
            axis_guide_block=_axis_guide_block_from_list(axis_guides),
        )
    if answer_mode_uses_tags(answer_mode):
        return PROMPT_B_EXTRACT_SUPPORT_DIAGRAM_PLUS_TAGS.format(
            query=query,
            readback=readback,
            tags=", ".join(tags),
            priority_preamble=_priority_preamble(prompt_priority, "b"),
            axis_guide_block=_axis_guide_block_from_list(axis_guides),
        )
    return PROMPT_B_EXTRACT_SUPPORT_DIAGRAM_ONLY.format(
        query=query,
        readback=readback,
        priority_preamble=_priority_preamble(prompt_priority, "b"),
        axis_guide_block=_axis_guide_block_from_list(axis_guides),
    )


def build_phase_b_answer_from_support_prompt(
    query: str,
    readback: str,
    support_packet: str,
    tags: List[str],
    answer_mode: str,
    prompt_priority: str,
) -> str:
    if not has_query_text(query):
        return build_phase_b_align_prompt(
            query=query,
            readback=support_packet,
            tags=tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
        )
    if answer_mode_uses_tags(answer_mode):
        return PROMPT_B_ANSWER_FROM_SUPPORT_DIAGRAM_PLUS_TAGS.format(
            query=query,
            readback=readback,
            support_packet=support_packet,
            tags=", ".join(tags),
            priority_preamble=_priority_preamble(prompt_priority, "b"),
        )
    return PROMPT_B_ANSWER_FROM_SUPPORT_DIAGRAM_ONLY.format(
        query=query,
        readback=readback,
        support_packet=support_packet,
        priority_preamble=_priority_preamble(prompt_priority, "b"),
    )


def build_phase_c_prompt(
    diagram: str,
    tags: List[str],
    answer_mode: str,
    prompt_priority: str,
    diagram_readback: str = "",
    diagram_support: str = "",
) -> str:
    if prompt_priority == "method_first":
        if (diagram_support or "").strip():
            return PROMPT_C_SUPPORT_SUMMARY.format(
                support_packet=diagram_support.strip(),
                priority_preamble=_priority_preamble(prompt_priority, "c"),
            )
        return PROMPT_C_READBACK_SUMMARY.format(
            readback=diagram_readback.strip() or "1) 支持されること: なし\n2) 支持されないこと: なし\n3) 未決定なこと: 未決定",
            priority_preamble=_priority_preamble(prompt_priority, "c"),
        )
    if answer_mode_uses_tags(answer_mode):
        return PROMPT_C_DIAGRAM_PLUS_TAGS.format(
            tags=", ".join(tags),
            priority_preamble=_priority_preamble(prompt_priority, "c"),
        )
    return PROMPT_C_DIAGRAM_ONLY.format(
        priority_preamble=_priority_preamble(prompt_priority, "c"),
    )

def phase_a_once(
    llm: BaseLLMClient,
    query: str,
    prompt_priority: str = "balanced",
    phase_a_axis_binding: bool = False,
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_a_prompt(query=query, prompt_priority=prompt_priority, phase_a_axis_binding=phase_a_axis_binding)
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": build_system_a(prompt_priority, phase_a_axis_binding=phase_a_axis_binding)},
        {"role": "user", "content": prompt},
    ])

def phase_a(
    llm: BaseLLMClient,
    query: str,
    *,
    prompt_priority: str = "balanced",
    phase_a_axis_binding: bool = False,
    max_attempts: int = 3,
    min_tags: int = 1,
    allow_tag_label_exception: bool = False,
    diagram_max_lines: int = 16,
    diagram_max_width: int = 64,
) -> Tuple[PhaseAResult, int, List[str], List[str]]:
    """
    Phase A をバリデーション付きで実行し、失敗したら自動リトライ。
    戻り値: (PhaseAResult, attempts_used, last_errors)
    """
    last_result = PhaseAResult(seed="", diagram="", tags=[], unknown_tags=[], diagram_hash=sha256_text(""))
    last_errors: List[str] = []
    raw_attempts: List[str] = []
    min_recurring_motifs = 3 if (phase_a_axis_binding and prompt_priority == "method_first") else 2

    repair_prefix = ""
    attempts_used = 0

    for attempt in range(1, int(max_attempts) + 1):
        attempts_used = attempt
        raw = phase_a_once(
            llm,
            query,
            prompt_priority=prompt_priority,
            phase_a_axis_binding=phase_a_axis_binding,
            repair_prefix=repair_prefix,
        )
        raw_attempts.append(raw)

        seed = clip_seed(extract_block(raw, "SEED"))
        diagram = clip_diagram(extract_block(raw, "DIAGRAM"), max_lines=diagram_max_lines, max_width=diagram_max_width)
        tags_raw = extract_block(raw, "TAGS")
        tags, unknown = parse_tags(tags_raw)

        last_result = PhaseAResult(
            seed=seed,
            diagram=diagram,
            tags=tags,
            unknown_tags=unknown,
            diagram_hash=sha256_text(diagram),
            used_fallback_tags=False,
        )

        last_errors = validate_phase_a(
            seed=seed,
            diagram=diagram,
            tags=tags,
            min_tags=min_tags,
            allow_tag_label_exception=allow_tag_label_exception,
            min_recurring_motifs=min_recurring_motifs,
        )

        if not last_errors:
            return last_result, attempts_used, [], raw_attempts

        repair_prefix = build_phase_a_repair_prefix(last_errors, allow_tag_label_exception)

    return last_result, attempts_used, last_errors, raw_attempts

def phase_b_direct_once(
    llm: BaseLLMClient,
    query: str,
    diagram: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "balanced",
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_b_prompt(query=query, tags=tags, answer_mode=answer_mode, prompt_priority=prompt_priority)
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    prompt = prompt + "\n\n" + _diagram_block(diagram)
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_b_readback_once(
    llm: BaseLLMClient,
    diagram: str,
    *,
    prompt_priority: str = "method_first",
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_b_readback_prompt(prompt_priority=prompt_priority)
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    prompt = prompt + "\n\n" + _diagram_block(diagram)
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_b_readback(
    llm: BaseLLMClient,
    diagram: str,
    *,
    prompt_priority: str = "method_first",
    max_attempts: int = 2,
) -> str:
    repair_prefix = ""
    last_output = ""
    for _ in range(max(1, int(max_attempts))):
        raw = phase_b_readback_once(
            llm,
            diagram,
            prompt_priority=prompt_priority,
            repair_prefix=repair_prefix,
        )
        last_output = sanitize_phase_b_readback_output(raw)
        errors = validate_phase_b_readback_output(raw)
        if not errors:
            return last_output
        repair_prefix = build_phase_b_readback_repair_prefix(errors)
    return last_output


def phase_b_align_once(
    llm: BaseLLMClient,
    query: str,
    readback: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "method_first",
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_b_align_prompt(
        query=query,
        readback=readback,
        tags=tags,
        answer_mode=answer_mode,
        prompt_priority=prompt_priority,
    )
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_b_support_once(
    llm: BaseLLMClient,
    query: str,
    readback: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "method_first",
    axis_guides: Optional[List[str]] = None,
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_b_support_prompt(
        query=query,
        readback=readback,
        tags=tags,
        answer_mode=answer_mode,
        prompt_priority=prompt_priority,
        axis_guides=axis_guides,
    )
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_b_support(
    llm: BaseLLMClient,
    query: str,
    readback: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "method_first",
    axis_guides: Optional[List[str]] = None,
    max_attempts: int = 2,
) -> str:
    repair_prefix = ""
    last_output = ""
    for _ in range(max(1, int(max_attempts))):
        raw = phase_b_support_once(
            llm,
            query,
            readback,
            tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            axis_guides=axis_guides,
            repair_prefix=repair_prefix,
        )
        last_output = sanitize_phase_b_support_output(raw)
        errors = validate_phase_b_support_output(raw)
        if not errors:
            return last_output
        repair_prefix = build_phase_b_support_repair_prefix(errors)
    return last_output


def phase_b_answer_from_support_once(
    llm: BaseLLMClient,
    query: str,
    readback: str,
    support_packet: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "method_first",
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_b_answer_from_support_prompt(
        query=query,
        readback=readback,
        support_packet=support_packet,
        tags=tags,
        answer_mode=answer_mode,
        prompt_priority=prompt_priority,
    )
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_b(
    llm: BaseLLMClient,
    query: str,
    diagram: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "balanced",
    axis_guides: Optional[List[str]] = None,
    max_attempts: int = 2,
) -> Tuple[str, str, str]:
    if prompt_priority == "method_first":
        readback = phase_b_readback(
            llm,
            diagram,
            prompt_priority=prompt_priority,
            max_attempts=max_attempts,
        )
        support_packet = phase_b_support(
            llm,
            query,
            readback,
            tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            axis_guides=axis_guides,
            max_attempts=max_attempts,
        )
        repair_prefix = ""
        last_output = ""
        for _ in range(max(1, int(max_attempts))):
            raw = phase_b_answer_from_support_once(
                llm,
                query,
                readback,
                support_packet,
                tags,
                answer_mode=answer_mode,
                prompt_priority=prompt_priority,
                repair_prefix=repair_prefix,
            )
            last_output = sanitize_phase_b_output(raw)
            errors = validate_phase_b_output(
                raw,
                prompt_priority=prompt_priority,
                require_method_first_label=False,
            )
            if not errors:
                return last_output, readback, support_packet
            repair_prefix = build_phase_b_repair_prefix(errors)
        return last_output, readback, support_packet

    repair_prefix = ""
    last_output = ""
    for _ in range(max(1, int(max_attempts))):
        raw = phase_b_direct_once(
            llm,
            query,
            diagram,
            tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            repair_prefix=repair_prefix,
        )
        last_output = sanitize_phase_b_output(raw)
        errors = validate_phase_b_output(
            raw,
            prompt_priority=prompt_priority,
            require_method_first_label=has_query_text(query),
        )
        if not errors:
            return last_output, "", ""
        repair_prefix = build_phase_b_repair_prefix(errors)
    return last_output, "", ""


def phase_c_once(
    llm: BaseLLMClient,
    diagram: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "balanced",
    diagram_readback: str = "",
    diagram_support: str = "",
    repair_prefix: str = "",
) -> str:
    prompt = build_phase_c_prompt(
        diagram=diagram,
        tags=tags,
        answer_mode=answer_mode,
        prompt_priority=prompt_priority,
        diagram_readback=diagram_readback,
        diagram_support=diagram_support,
    )
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    if prompt_priority != "method_first":
        prompt = prompt + "\n\n" + _diagram_block(diagram)
    return llm.chat([
        {"role": "system", "content": build_system_b(prompt_priority)},
        {"role": "user", "content": prompt},
    ])


def phase_c(
    llm: BaseLLMClient,
    diagram: str,
    tags: List[str],
    *,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "balanced",
    diagram_readback: str = "",
    diagram_support: str = "",
    max_attempts: int = 2,
) -> str:
    repair_prefix = ""
    last_output = ""
    for _ in range(max(1, int(max_attempts))):
        raw = phase_c_once(
            llm,
            diagram,
            tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            diagram_readback=diagram_readback,
            diagram_support=diagram_support,
            repair_prefix=repair_prefix,
        )
        last_output = sanitize_phase_c_output(raw)
        errors = validate_phase_c_output(raw)
        if not errors:
            return last_output
        repair_prefix = build_phase_c_repair_prefix(errors)
    return last_output


def annotate_axis_adherence_judgment(
    judgment: ConditionMatrixJudgment,
    entries: List[ConditionMatrixEntry],
) -> None:
    assessed = [
        entry
        for entry in entries
        if entry.status != CONDITION_STATUS_SKIPPED and entry.axis_adherence_score >= 0.0
    ]
    if not assessed:
        return

    def _axis_is_generic(entry: ConditionMatrixEntry) -> bool:
        label = (entry.axis_adherence_label or "").strip().lower()
        return label in ("generic", "off_axis") or entry.axis_adherence_score < 0.45

    generic_like = [entry for entry in assessed if _axis_is_generic(entry)]
    semantic_survivors = [
        entry
        for entry in assessed
        if entry.status in (CONDITION_STATUS_SURVIVES, CONDITION_STATUS_BORDERLINE)
    ]
    generic_survivors = [entry for entry in semantic_survivors if _axis_is_generic(entry)]
    retained = [
        entry
        for entry in assessed
        if (entry.axis_adherence_label or "").strip().lower() in ("strong", "partial")
        and entry.axis_adherence_score >= 0.45
    ]
    cross_entry = next((entry for entry in assessed if entry.condition_id == "cross_diagram"), None)

    if len(generic_like) == len(assessed):
        judgment.axis_adherence_verdict = "axis_generic_collapse"
        judgment.axis_adherence_reason = (
            "All assessed condition rows were generic/off_axis or below the axis threshold, "
            "so semantic similarity may be coming from cautious structure descriptions rather than retained AXIS_GUIDE axes."
        )
        return

    if (
        semantic_survivors
        and len(generic_survivors) == len(semantic_survivors)
        and len(generic_survivors) > 0
    ):
        judgment.axis_adherence_verdict = "semantic_survives_but_axis_generic"
        judgment.axis_adherence_reason = (
            "Rows that survived semantically did not retain concrete AXIS_GUIDE axes, "
            "so the survive pattern should not be read as strong diagrammatic axis transfer."
        )
        return

    if (
        cross_entry is not None
        and cross_entry.status in (CONDITION_STATUS_SURVIVES, CONDITION_STATUS_BORDERLINE)
        and _axis_is_generic(cross_entry)
    ):
        judgment.axis_adherence_verdict = "cross_survives_axis_generic"
        judgment.axis_adherence_reason = (
            "CROSS_DIAGRAM survived semantically but its axis adherence is generic, "
            "so portability may be driven by abstract connection language rather than the target axes."
        )
        return

    if retained:
        judgment.axis_adherence_verdict = "axis_adherence_present"
        judgment.axis_adherence_reason = (
            "At least one assessed condition retained concrete AXIS_GUIDE axes above the axis threshold."
        )
        return

    judgment.axis_adherence_verdict = "mixed_axis_adherence"
    judgment.axis_adherence_reason = "Axis-adherence rows were mixed and did not produce a clean secondary split."


def build_condition_matrix_judgment(
    entries: List[ConditionMatrixEntry],
    *,
    pass_threshold: float,
    soft_threshold: float,
) -> ConditionMatrixJudgment:
    by_id = {entry.condition_id: entry for entry in entries}

    def _status(condition_id: str) -> str:
        entry = by_id.get(condition_id)
        return entry.status if entry is not None else CONDITION_STATUS_SKIPPED

    no_query_strict_status = _status("no_query_strict")
    if no_query_strict_status == CONDITION_STATUS_SKIPPED:
        no_query_strict_status = _status("no_query")

    judgment = ConditionMatrixJudgment(
        pass_threshold=float(pass_threshold),
        soft_threshold=float(soft_threshold),
        no_query_strict_status=no_query_strict_status,
        no_query_with_axis_status=_status("no_query_with_axis"),
        no_query_status=no_query_strict_status,
        equiv_diagram_status=_status("equiv_diagram"),
        cross_diagram_status=_status("cross_diagram"),
    )
    annotate_axis_adherence_judgment(judgment, entries)

    if (
        judgment.no_query_strict_status == CONDITION_STATUS_FAILS
        and judgment.no_query_with_axis_status in (CONDITION_STATUS_SURVIVES, CONDITION_STATUS_BORDERLINE)
    ):
        judgment.overall_verdict = "axis_guide_dependent"
        judgment.overall_reason = (
            "NO_QUERY_STRICT collapsed but NO_QUERY_WITH_AXIS survived or nearly survived, "
            "so the answer appears to depend on the abstract AXIS_GUIDE channel rather than the full query text alone."
        )
        return judgment

    if judgment.no_query_strict_status == CONDITION_STATUS_FAILS:
        judgment.overall_verdict = "query_or_proposition_dependent"
        judgment.overall_reason = "NO_QUERY_STRICT collapsed first, so the answer still appears to depend mainly on the query/proposition channel."
        return judgment

    if judgment.equiv_diagram_status == CONDITION_STATUS_FAILS:
        judgment.overall_verdict = "surface_form_dependent"
        judgment.overall_reason = "An equivalent-looking transform broke the answer, so the diagram effect appears tied to surface appearance more than topology."
        return judgment

    if judgment.cross_diagram_status == CONDITION_STATUS_FAILS:
        judgment.overall_verdict = "diagram_semantics_present_but_not_portable"
        judgment.overall_reason = "Cross-diagram transfer broke while earlier conditions held, so diagrammatic meaning may exist but is not portable across problems."
        return judgment

    statuses = [entry.status for entry in entries if entry.status != CONDITION_STATUS_SKIPPED]
    if statuses and all(status == CONDITION_STATUS_SURVIVES for status in statuses):
        if {"no_query_strict", "equiv_diagram", "cross_diagram"}.issubset(by_id.keys()):
            judgment.overall_verdict = "diagrammatic_semantics_strong"
            judgment.overall_reason = "NO_QUERY_STRICT, EQUIV_DIAGRAM, and CROSS_DIAGRAM all survived, which supports a strong diagrammatic meaning-operation hypothesis."
        else:
            judgment.overall_verdict = "requested_conditions_survive"
            judgment.overall_reason = "All requested condition-matrix checks survived."
        return judgment

    if any(status == CONDITION_STATUS_BORDERLINE for status in statuses):
        judgment.overall_verdict = "mixed_or_borderline"
        judgment.overall_reason = "At least one condition landed in the borderline band, so the meaning-carrier split is not yet clean."
        return judgment

    judgment.overall_verdict = "mixed_or_inconclusive"
    judgment.overall_reason = "The condition matrix did not produce a clean separation."
    return judgment


def run_condition_matrix(
    llm: BaseLLMClient,
    *,
    problem_id: str,
    query: str,
    diagram: str,
    tags: List[str],
    answer_mode: str,
    prompt_priority: str,
    baseline_answer: str,
    temperature_test: float,
    requested_conditions: List[str],
    compare_mode: str,
    equiv_diagram_mode: str,
    cross_source_mode: str,
    cross_problem_requested: Optional[str],
    save_dir: Optional[Path],
    swap_bank_path: Optional[str],
    pass_threshold: float,
    soft_threshold: float,
    run_seed: Optional[int],
    phase_a_axis_guide: Optional[List[str]] = None,
) -> ConditionMatrixResult:
    entries: List[ConditionMatrixEntry] = []
    tag_mode_label = "full" if answer_mode_uses_tags(answer_mode) else "ignored_by_answer_mode"
    seed_int = int(sha256_text(diagram)[:8], 16) if diagram else 0
    if run_seed is not None:
        seed_int = (seed_int ^ (int(run_seed) & 0xFFFFFFFF)) & 0xFFFFFFFF
    compare_mode = parse_condition_compare_mode(compare_mode)

    for condition_id in requested_conditions:
        run_query = query
        run_diagram = diagram
        run_tags = list(tags)
        run_axis_guides: Optional[List[str]] = phase_a_axis_guide
        diagram_mode = "base"
        diagram_source = ""
        diagram_source_kind = ""
        source_problem_id = ""
        notes = ""
        label = condition_id.upper()

        if condition_id in ("no_query", "no_query_strict"):
            run_query = ""
            run_axis_guides = None
            label = "NO_QUERY_STRICT"
            notes = "Phase B receives no query text and no AXIS_GUIDE; only DIAGRAM/TAGS remain."
        elif condition_id == "no_query_with_axis":
            run_query = ""
            run_axis_guides = list(phase_a_axis_guide or [])
            label = "NO_QUERY_WITH_AXIS"
            notes = "Phase B receives no query text but keeps the abstract AXIS_GUIDE from Phase A."
        elif condition_id == "equiv_diagram":
            run_diagram = transform_equivalent_diagram(diagram, mode=equiv_diagram_mode)
            diagram_mode = "equiv_diagram"
            diagram_source = f"equiv:{equiv_diagram_mode}"
            diagram_source_kind = "equiv_transform"
            notes = "Topology-preserving appearance transform applied to the baseline diagram."
        elif condition_id == "cross_diagram":
            cross_diagram, cross_source, cross_problem_id = find_cross_diagram(
                problem_id,
                save_dir,
                sha256_text(diagram),
                source_mode=cross_source_mode,
                preferred_problem_id=cross_problem_requested,
                swap_bank_path=swap_bank_path,
                seed=seed_int,
            )
            if cross_diagram is None or cross_source is None or cross_problem_id is None:
                entries.append(
                    ConditionMatrixEntry(
                        condition_id=condition_id,
                        label=label,
                        query_mode="full",
                        diagram_mode="cross_diagram",
                        tags_mode=tag_mode_label,
                        diagram_hash="",
                        answer="(skipped: no cross-diagram source found)",
                        similarity=-1.0,
                        status=CONDITION_STATUS_SKIPPED,
                        compare_mode=compare_mode,
                        axis_guide_mode="full" if run_axis_guides else "none",
                        diagram_source="",
                        diagram_source_kind="",
                        source_problem_id="",
                        notes="No cross-problem diagram source was available.",
                    )
                )
                continue
            run_diagram = cross_diagram
            diagram_mode = "cross_diagram"
            diagram_source = cross_source
            diagram_source_kind = "bank" if cross_source.startswith("bank:") else "saved"
            source_problem_id = cross_problem_id
            notes = "Different-problem diagram reused against the current query."
        else:
            raise ValueError(f"Unhandled condition matrix condition: {condition_id}")

        answer_variant, _, _ = phase_b(
            llm,
            run_query,
            run_diagram,
            run_tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            axis_guides=run_axis_guides,
        )
        surface_similarity = similarity_ratio(baseline_answer, answer_variant)
        semantic_label: Optional[str] = None
        semantic_score: Optional[float] = None
        semantic_reason = ""

        if compare_mode in ("semantic_llm", "hybrid"):
            semantic_label, semantic_score, semantic_reason = semantic_similarity(
                llm,
                query=query,
                baseline_answer=baseline_answer,
                candidate_answer=answer_variant,
            )

        axis_label = ""
        axis_score = -1.0
        axis_axes = ""
        axis_reason = ""
        if phase_a_axis_guide and compare_mode in ("semantic_llm", "hybrid"):
            adherence_label, adherence_score, adherence_axes, adherence_reason = axis_adherence(
                llm,
                query=query,
                axis_guides=list(phase_a_axis_guide),
                baseline_answer=baseline_answer,
                candidate_answer=answer_variant,
            )
            axis_label = str(adherence_label or "")
            axis_score = adherence_score if adherence_score is not None else -1.0
            axis_axes = adherence_axes
            axis_reason = adherence_reason

        if compare_mode == "surface":
            similarity = surface_similarity
        elif semantic_score is not None:
            similarity = semantic_score
        else:
            similarity = surface_similarity

        status = classify_condition_similarity(
            similarity,
            pass_threshold=pass_threshold,
            soft_threshold=soft_threshold,
        )
        entries.append(
            ConditionMatrixEntry(
                condition_id=condition_id,
                label=label,
                query_mode="none" if not has_query_text(run_query) else "full",
                diagram_mode=diagram_mode,
                tags_mode=tag_mode_label,
                diagram_hash=sha256_text(run_diagram),
                answer=answer_variant,
                similarity=similarity,
                status=status,
                compare_mode=compare_mode if (compare_mode == "surface" or semantic_score is not None) else "surface_fallback",
                axis_guide_mode="full" if run_axis_guides else "none",
                surface_similarity=surface_similarity,
                semantic_similarity=(semantic_score if semantic_score is not None else -1.0),
                comparison_label=str(semantic_label or ""),
                comparison_reason=semantic_reason,
                axis_adherence_label=axis_label,
                axis_adherence_score=axis_score,
                axis_adherence_axes=axis_axes,
                axis_adherence_reason=axis_reason,
                diagram_source=diagram_source,
                diagram_source_kind=diagram_source_kind,
                source_problem_id=source_problem_id,
                notes=notes,
            )
        )

    return ConditionMatrixResult(
        enabled=True,
        temperature_test=float(temperature_test),
        requested_conditions=list(requested_conditions),
        compare_mode=compare_mode,
        equiv_diagram_mode=str(equiv_diagram_mode),
        cross_source_mode=str(cross_source_mode),
        cross_problem_requested=str(cross_problem_requested or ""),
        baseline_answer=baseline_answer,
        entries=entries,
        judgment=build_condition_matrix_judgment(
            entries,
            pass_threshold=pass_threshold,
            soft_threshold=soft_threshold,
        ),
    )


# ======================
# 6) 実行 + 保存 + テスト
# ======================

def run_once(
    llm: BaseLLMClient,
    provider: str,
    model: str,
    problem_id: str,
    answer_mode: str = "diagram_only",
    prompt_priority: str = "balanced",
    run_seed: Optional[int] = None,
    field_metrics: bool = False,
    field_window: int = 128,
    field_time_layer: str = "last",
    field_time_every: int = 5,
    field_fit_k: int = 64,
    field_layers: str = "all",
    save_dir: Optional[Path] = None,
    print_diagram: bool = False,
    run_tests: bool = False,
    run_condition_matrix_suite: bool = False,
    condition_matrix_conditions: Optional[List[str]] = None,
    condition_compare_mode: str = "semantic_llm",
    equiv_diagram_mode: str = "vertical_flip_remap",
    cross_diagram_source_mode: str = "auto",
    cross_problem_id: Optional[str] = None,
    condition_pass_threshold: float = 0.55,
    condition_soft_threshold: float = 0.35,
    test_mode: str = "full",          # "lite" or "full"
    contrib_tests: bool = True,       # 2x2 因子分解（呼び出し回数が増える）
    diagram_tests: bool = True,       # corruption/swap（呼び出し回数が増える）
    diagram_corrupt_mode: str = "noise",
    diagram_corrupt_rate: float = 0.12,
    diagram_swap_mode: str = "auto",  # auto(bank first) / bank_only / saved_only
    swap_bank_path: Optional[str] = None,
    skip_caption: bool = False,
    enable_fallback_tags: bool = True,
    tamper_remove: str = "gap",
    tamper_add: str = "proxy",
    phase_a_max_attempts: int = 3,
    phase_a_min_tags: int = 1,
    phase_a_axis_binding: bool = False,
    allow_tag_label_exception: bool = False,
    allow_invalid_phase_a: bool = False,
    temperature_a: float = 0.7,
    temperature_answer: float = 0.7,
    temperature_test: float = 0.0,
) -> RunResult:
    if problem_id not in PROBLEMS:
        raise ValueError(f"Unknown problem_id: {problem_id}. Use one of {list(PROBLEMS.keys())}")

    if run_seed is not None:
        random.seed(int(run_seed))
    tag_sensitive = answer_mode_uses_tags(answer_mode)

    query, meta = get_problem(problem_id)
    fallback_tags = meta.get("fallback_tags", [])
    preferred_remove = meta.get("tamper_remove", tamper_remove)
    preferred_add = meta.get("tamper_add", tamper_add)
    min_tags_effective = int(phase_a_min_tags) if tag_sensitive else 0
    phase_a_axis_guide = _axis_binding_guides(prompt_priority, phase_a_axis_binding, query)

    # Phase A（ASCII思考） + validate/retry
    with override_temperature(llm, temperature_a):
        a, attempts_used, phase_a_errors, phase_a_raw_attempts = phase_a(
            llm, query,
            prompt_priority=prompt_priority,
            phase_a_axis_binding=phase_a_axis_binding,
            max_attempts=phase_a_max_attempts,
            min_tags=min_tags_effective,
            allow_tag_label_exception=allow_tag_label_exception,
        )

    if phase_a_errors and not allow_invalid_phase_a:
        if save_dir:
            failure_json = save_phase_a_failure_artifacts(
                save_dir=save_dir,
                provider=provider,
                model=model,
                problem_id=problem_id,
                query=query,
                answer_mode=answer_mode,
                prompt_priority=prompt_priority,
                phase_a_axis_binding=phase_a_axis_binding,
                phase_a_axis_guide=phase_a_axis_guide,
                run_seed=run_seed,
                attempts_used=attempts_used,
                phase_a_errors=phase_a_errors,
                phase_a_result=a,
                raw_attempts=phase_a_raw_attempts,
            )
            print(f"phase_a_failure_artifacts: {failure_json}")
        joined = "; ".join(phase_a_errors)
        raise RuntimeError(f"Phase A validation failed after {attempts_used} attempts: {joined}")

    # TAGSが空なら fallback を注入（任意）
    if enable_fallback_tags and (not a.tags) and fallback_tags:
        a.tags = [t for t in fallback_tags if t in TAG_VOCAB]
        a.used_fallback_tags = True

    # Phase B（回答）
    fm: Optional[FieldMetrics] = None
    diagram_readback = ""
    diagram_support = ""
    with override_temperature(llm, temperature_answer):
        if field_metrics:
            if provider != "hf":
                raise ValueError("--field-metrics is only supported with --provider hf (local HF models).")
            cfg = {
                "window": int(field_window),
                "time_layer": str(field_time_layer),
                "time_every": int(field_time_every),
                "fit_k": int(field_fit_k),
                "layers": str(field_layers),
            }
            with override_field_trace(llm, cfg):
                answer, diagram_readback, diagram_support = phase_b(
                    llm,
                    query,
                    a.diagram,
                    a.tags,
                    answer_mode=answer_mode,
                    prompt_priority=prompt_priority,
                    axis_guides=phase_a_axis_guide,
                )
            fm = getattr(llm, "last_field_metrics", None)
        else:
            answer, diagram_readback, diagram_support = phase_b(
                llm,
                query,
                a.diagram,
                a.tags,
                answer_mode=answer_mode,
                prompt_priority=prompt_priority,
                axis_guides=phase_a_axis_guide,
            )

    # Phase C（1行説明）
    with override_temperature(llm, temperature_answer):
        caption = "(skipped)" if skip_caption else phase_c(
            llm,
            a.diagram,
            a.tags,
            answer_mode=answer_mode,
            prompt_priority=prompt_priority,
            diagram_readback=diagram_readback,
            diagram_support=diagram_support,
        )

    tests: Optional[TestResult] = None
    condition_matrix_result: Optional[ConditionMatrixResult] = None
    if run_tests or run_condition_matrix_suite:
        with override_temperature(llm, temperature_test):
            # base_answer_test（温度が同じなら再利用して節約）
            if abs(float(temperature_answer) - float(temperature_test)) < 1e-9:
                base_answer_test = answer
            else:
                base_answer_test, _, _ = phase_b(
                    llm,
                    query,
                    a.diagram,
                    a.tags,
                    answer_mode=answer_mode,
                    prompt_priority=prompt_priority,
                    axis_guides=phase_a_axis_guide,
                )

            if run_tests:
                # 2x2 contribution
                contrib = Contribution2x2(enabled=bool(contrib_tests))

                if tag_sensitive:
                    # NO_TAGS（= ablation）: contrib_tests に関わらず必ず取る
                    no_tags_answer, _, _ = phase_b(
                        llm,
                        query,
                        a.diagram,
                        [],
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    contrib.no_tags_answer = no_tags_answer
                    contrib.no_tags_similarity = similarity_ratio(base_answer_test, no_tags_answer)
                else:
                    contrib.no_tags_answer = "(skipped: answer_mode=diagram_only)"
                    contrib.no_tags_similarity = -1.0

                if contrib_tests:
                    no_diagram_answer, _, _ = phase_b(
                        llm,
                        query,
                        "",
                        a.tags,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    contrib.no_diagram_answer = no_diagram_answer
                    contrib.no_diagram_similarity = similarity_ratio(base_answer_test, no_diagram_answer)

                    neither_answer, _, _ = phase_b(
                        llm,
                        query,
                        "",
                        [],
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    contrib.neither_answer = neither_answer
                    contrib.neither_similarity = similarity_ratio(base_answer_test, neither_answer)

                # Diagram tests（corruption / swap）
                dtests = DiagramTests(enabled=bool(diagram_tests), corruption_mode=diagram_corrupt_mode, corruption_rate=float(diagram_corrupt_rate))
                if diagram_tests:
                    # corruption
                    seed_int = int(a.diagram_hash[:8], 16) if a.diagram_hash else 0
                    if run_seed is not None:
                        seed_int = (seed_int ^ (int(run_seed) & 0xFFFFFFFF)) & 0xFFFFFFFF
                    corrupt = corrupt_diagram(a.diagram, mode=diagram_corrupt_mode, rate=diagram_corrupt_rate, seed=seed_int)
                    dtests.corrupt_diagram_hash = sha256_text(corrupt)
                    corrupt_answer, _, _ = phase_b(
                        llm,
                        query,
                        corrupt,
                        a.tags,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    dtests.corrupt_answer = corrupt_answer
                    dtests.corrupt_similarity = similarity_ratio(base_answer_test, corrupt_answer)

                    # swap（save_dir から別diagramを拾える場合のみ）
                    swap_d, swap_src = find_swap_diagram(
                        problem_id,
                        save_dir,
                        a.diagram_hash,
                        swap_mode=diagram_swap_mode,
                        swap_bank_path=swap_bank_path,
                        seed=seed_int,
                    )
                    if swap_d is not None and swap_src is not None:
                        dtests.swap_used = True
                        dtests.swap_source = swap_src
                        dtests.swap_source_kind = "bank" if swap_src.startswith("bank:") else "saved"
                        dtests.swap_diagram_hash = sha256_text(swap_d)
                        swap_answer, _, _ = phase_b(
                            llm,
                            query,
                            swap_d,
                            a.tags,
                            answer_mode=answer_mode,
                            prompt_priority=prompt_priority,
                            axis_guides=phase_a_axis_guide,
                        )
                        dtests.swap_answer = swap_answer
                        dtests.swap_similarity = similarity_ratio(base_answer_test, swap_answer)
                else:
                    dtests = DiagramTests(enabled=False)

                # Tamper（存在しないタグをremoveしないように自動選択）
                rm_used = choose_remove_tag(a.tags, preferred_remove) if tag_sensitive else None
                ad_used = choose_add_tag(a.tags, preferred_add) if tag_sensitive else None

                if not tag_sensitive:
                    tests = TestResult(
                        test_mode=str(test_mode),
                        temperature_test=float(temperature_test),
                        base_answer_test=base_answer_test,
                        contrib=contrib,
                        diagram_tests=dtests,
                        tamper_remove_used=None,
                        tamper_add_used=None,
                        tamper_remove_answer="(skipped: answer_mode=diagram_only)",
                        tamper_remove_similarity=-1.0,
                        tamper_add_answer="(skipped: answer_mode=diagram_only)",
                        tamper_add_similarity=-1.0,
                        tamper_both_answer="(skipped: answer_mode=diagram_only)",
                        tamper_both_similarity=-1.0,
                    )
                elif test_mode == "lite":
                    tags_both = tamper_tags(a.tags, remove_tag=rm_used, add_tag=ad_used)
                    tamper_both_answer, _, _ = phase_b(
                        llm,
                        query,
                        a.diagram,
                        tags_both,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    tamper_both_sim = similarity_ratio(base_answer_test, tamper_both_answer)

                    tests = TestResult(
                        test_mode="lite",
                        temperature_test=float(temperature_test),
                        base_answer_test=base_answer_test,
                        contrib=contrib,
                        diagram_tests=dtests,
                        tamper_remove_used=rm_used,
                        tamper_add_used=ad_used,
                        tamper_remove_answer="(skipped)",
                        tamper_remove_similarity=-1.0,
                        tamper_add_answer="(skipped)",
                        tamper_add_similarity=-1.0,
                        tamper_both_answer=tamper_both_answer,
                        tamper_both_similarity=tamper_both_sim,
                    )
                else:
                    tags_remove = tamper_tags(a.tags, remove_tag=rm_used, add_tag=None)
                    tamper_remove_answer, _, _ = phase_b(
                        llm,
                        query,
                        a.diagram,
                        tags_remove,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    tamper_remove_sim = similarity_ratio(base_answer_test, tamper_remove_answer)

                    tags_add = tamper_tags(a.tags, remove_tag=None, add_tag=ad_used)
                    tamper_add_answer, _, _ = phase_b(
                        llm,
                        query,
                        a.diagram,
                        tags_add,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    tamper_add_sim = similarity_ratio(base_answer_test, tamper_add_answer)

                    tags_both = tamper_tags(a.tags, remove_tag=rm_used, add_tag=ad_used)
                    tamper_both_answer, _, _ = phase_b(
                        llm,
                        query,
                        a.diagram,
                        tags_both,
                        answer_mode=answer_mode,
                        prompt_priority=prompt_priority,
                        axis_guides=phase_a_axis_guide,
                    )
                    tamper_both_sim = similarity_ratio(base_answer_test, tamper_both_answer)

                    tests = TestResult(
                        test_mode="full",
                        temperature_test=float(temperature_test),
                        base_answer_test=base_answer_test,
                        contrib=contrib,
                        diagram_tests=dtests,
                        tamper_remove_used=rm_used,
                        tamper_add_used=ad_used,
                        tamper_remove_answer=tamper_remove_answer,
                        tamper_remove_similarity=tamper_remove_sim,
                        tamper_add_answer=tamper_add_answer,
                        tamper_add_similarity=tamper_add_sim,
                        tamper_both_answer=tamper_both_answer,
                        tamper_both_similarity=tamper_both_sim,
                    )

            if run_condition_matrix_suite:
                requested_conditions = list(condition_matrix_conditions or CONDITION_MATRIX_DEFAULT_CONDITIONS)
                condition_matrix_result = run_condition_matrix(
                    llm,
                    problem_id=problem_id,
                    query=query,
                    diagram=a.diagram,
                    tags=a.tags,
                    answer_mode=answer_mode,
                    prompt_priority=prompt_priority,
                    baseline_answer=base_answer_test,
                    temperature_test=temperature_test,
                    requested_conditions=requested_conditions,
                    compare_mode=condition_compare_mode,
                    equiv_diagram_mode=equiv_diagram_mode,
                    cross_source_mode=cross_diagram_source_mode,
                    cross_problem_requested=cross_problem_id,
                    save_dir=save_dir,
                    swap_bank_path=swap_bank_path,
                    pass_threshold=condition_pass_threshold,
                    soft_threshold=condition_soft_threshold,
                    run_seed=run_seed,
                    phase_a_axis_guide=phase_a_axis_guide,
                )

    result = RunResult(
        provider=provider,
        model=model,
        problem_id=problem_id,
        query=query,
        answer_mode=str(answer_mode),
        prompt_priority=str(prompt_priority),
        run_seed=(int(run_seed) if run_seed is not None else None),
        temperature_a=float(temperature_a),
        temperature_answer=float(temperature_answer),
        temperature_test=float(temperature_test),
        allow_tag_label_exception=bool(allow_tag_label_exception),
        phase_a_axis_binding=bool(phase_a_axis_binding),
        phase_a_axis_guide=list(phase_a_axis_guide),
        phase_a_attempts=int(attempts_used),
        phase_a_validation_errors=list(phase_a_errors),
        seed=a.seed,
        tags=a.tags,
        unknown_tags=a.unknown_tags,
        diagram_hash=a.diagram_hash,
        diagram_readback=diagram_readback,
        diagram_support=diagram_support,
        answer=answer,
        caption_1line=caption,
        tests=tests,
        condition_matrix=condition_matrix_result,
        field_metrics=fm,
    )

    # 保存（DIAGRAMはローカルのみ）
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = save_dir / f"{provider}_{problem_id}_{stamp}"

        (base.with_suffix(".diagram.txt")).write_text(a.diagram, encoding="utf-8")
        (base.with_suffix(".json")).write_text(
            json.dumps(dataclass_to_dict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 画面出力
    print("\n=== RUN ===")
    print(f"provider: {provider}")
    print(f"model: {model}")
    print(f"problem: {problem_id}")
    print(f"answer_mode: {answer_mode}")
    print(f"prompt_priority: {prompt_priority}")
    print(f"run_seed: {run_seed}")
    print(f"diagram_hash: {a.diagram_hash}")
    print(f"temp: A={temperature_a} / Answer={temperature_answer} / Test={temperature_test}")
    print(f"fallback_tags_used: {a.used_fallback_tags}")
    print(f"phase_a_attempts: {attempts_used}")
    print(f"allow_tag_label_exception: {allow_tag_label_exception}")
    print(f"phase_a_axis_binding: {phase_a_axis_binding}")

    if phase_a_axis_guide:
        print("\n[AXIS_GUIDE]")
        for idx, guide in enumerate(phase_a_axis_guide, start=1):
            print(f"A{idx}. {guide}")

    if phase_a_errors:
        print("\n[PHASE_A_VALIDATION_ERRORS]")
        for e in phase_a_errors:
            print(f"- {e}")

    print("\n[SEED]")
    print(a.seed or "(empty)")

    print("\n[TAGS]")
    print(", ".join(a.tags) if a.tags else "(none)")
    if a.unknown_tags:
        print("\n[unknown_tags]")
        print(", ".join(a.unknown_tags))

    if print_diagram:
        print("\n[DIAGRAM]")
        print(a.diagram)

    if diagram_readback:
        print("\n[READBACK]")
        print(diagram_readback)

    if diagram_support:
        print("\n[SUPPORT_PACKET]")
        print(diagram_support)

    print("\n[ANSWER]")
    print(answer)

    print("\n[CAPTION_1LINE]")
    print(caption)

    if tests:
        print("\n=== TESTS ===")
        print(f"mode: {tests.test_mode}")
        print(f"test_temperature: {tests.temperature_test}")
        print(f"tamper_remove_used: {tests.tamper_remove_used}")
        print(f"tamper_add_used: {tests.tamper_add_used}")

        # 2x2 contribution
        print("\n=== CONTRIBUTION (2x2) ===")
        if tag_sensitive:
            print(f"FULL vs NO_TAGS   : {tests.contrib.no_tags_similarity:.3f}")
        else:
            print("FULL vs NO_TAGS   : (skipped: answer_mode=diagram_only)")
        if tests.contrib.enabled:
            print(f"FULL vs NO_DIAGRAM: {tests.contrib.no_diagram_similarity:.3f}")
            print(f"FULL vs NEITHER   : {tests.contrib.neither_similarity:.3f}")
        else:
            print("(contrib tests disabled)")

        # Diagram tests
        print("\n=== DIAGRAM TESTS ===")
        if tests.diagram_tests.enabled:
            print(f"corruption mode: {tests.diagram_tests.corruption_mode} (rate={tests.diagram_tests.corruption_rate})")
            print(f"FULL vs CORRUPT  : {tests.diagram_tests.corrupt_similarity:.3f}")
            if tests.diagram_tests.swap_used:
                print(f"FULL vs SWAP     : {tests.diagram_tests.swap_similarity:.3f}")
                print(f"swap_source: {tests.diagram_tests.swap_source}")
                if tests.diagram_tests.swap_source_kind:
                    print(f"swap_source_kind: {tests.diagram_tests.swap_source_kind}")
            else:
                print("(swap: not used)")
        else:
            print("(diagram tests disabled)")

        if tag_sensitive:
            # 従来の見え方（Ablation=NO_TAGS）
            print(f"\nAblation (TAGS=[]): similarity={tests.contrib.no_tags_similarity:.3f}")
            print(tests.contrib.no_tags_answer)
        else:
            print("\nAblation (TAGS=[]): skipped (answer_mode=diagram_only)")

        if tag_sensitive and tests.test_mode == "full":
            print(f"\nTamper remove: similarity={tests.tamper_remove_similarity:.3f}")
            print(tests.tamper_remove_answer)

            print(f"\nTamper add: similarity={tests.tamper_add_similarity:.3f}")
            print(tests.tamper_add_answer)

        if tag_sensitive:
            print(f"\nTamper both: similarity={tests.tamper_both_similarity:.3f}")
            print(tests.tamper_both_answer)
        else:
            print("\nTamper both: skipped (answer_mode=diagram_only)")

        # 超雑なフラグ（目安）
        if tag_sensitive and tests.contrib.no_tags_similarity > 0.85:
            print("\n[WARN] NO_TAGSでも答えが似すぎ → TAGSが飾り/問だけで結論が出てる疑い")
        if tests.contrib.enabled and tests.contrib.no_diagram_similarity > 0.90:
            print("[WARN] NO_DIAGRAMでも答えがほぼ同じ → DIAGRAMが飾りの疑い")
        if tests.diagram_tests.enabled and tests.diagram_tests.corrupt_similarity > 0.90:
            print("[WARN] CORRUPTでも答えがほぼ同じ → DIAGRAMを読んでいない可能性")
        if tag_sensitive and tests.tamper_both_similarity > 0.90:
            print("[WARN] タグ改ざんでも答えがほぼ同じ → '図形→タグ→答え'の因果が弱い可能性")

    if condition_matrix_result:
        print("\n=== CONDITION MATRIX ===")
        print(f"test_temperature: {condition_matrix_result.temperature_test}")
        print(f"requested_conditions: {', '.join(condition_matrix_result.requested_conditions)}")
        print(f"compare_mode: {condition_matrix_result.compare_mode}")
        print(f"equiv_diagram_mode: {condition_matrix_result.equiv_diagram_mode}")
        print(f"cross_source_mode: {condition_matrix_result.cross_source_mode}")
        if condition_matrix_result.cross_problem_requested:
            print(f"cross_problem_requested: {condition_matrix_result.cross_problem_requested}")

        for entry in condition_matrix_result.entries:
            sim_str = f"{entry.similarity:.3f}" if entry.similarity >= 0.0 else "(skipped)"
            print(
                f"{entry.label}: status={entry.status} similarity={sim_str} "
                f"compare={entry.compare_mode} query={entry.query_mode} diagram={entry.diagram_mode} "
                f"tags={entry.tags_mode} axis={entry.axis_guide_mode or 'none'}"
            )
            if entry.surface_similarity >= 0.0:
                print(f"  surface_similarity: {entry.surface_similarity:.3f}")
            if entry.semantic_similarity >= 0.0:
                print(f"  semantic_similarity: {entry.semantic_similarity:.3f} ({entry.comparison_label})")
            if entry.comparison_reason:
                print(f"  comparison_reason: {entry.comparison_reason}")
            if entry.axis_adherence_score >= 0.0:
                print(f"  axis_adherence: {entry.axis_adherence_score:.3f} ({entry.axis_adherence_label})")
            if entry.axis_adherence_axes:
                print(f"  axis_axes: {entry.axis_adherence_axes}")
            if entry.axis_adherence_reason:
                print(f"  axis_reason: {entry.axis_adherence_reason}")
            if entry.source_problem_id:
                print(f"  source_problem_id: {entry.source_problem_id}")
            if entry.diagram_source:
                print(f"  diagram_source: {entry.diagram_source}")
            print(f"  answer: {entry.answer}")

        if condition_matrix_result.judgment:
            print("\n=== CONDITION MATRIX JUDGMENT ===")
            print(f"no_query_strict_status: {condition_matrix_result.judgment.no_query_strict_status}")
            print(f"no_query_with_axis_status: {condition_matrix_result.judgment.no_query_with_axis_status}")
            print(f"no_query_status: {condition_matrix_result.judgment.no_query_status}")
            print(f"equiv_diagram_status: {condition_matrix_result.judgment.equiv_diagram_status}")
            print(f"cross_diagram_status: {condition_matrix_result.judgment.cross_diagram_status}")
            print(f"overall_verdict: {condition_matrix_result.judgment.overall_verdict}")
            if condition_matrix_result.judgment.overall_reason:
                print(condition_matrix_result.judgment.overall_reason)
            if condition_matrix_result.judgment.axis_adherence_verdict:
                print(f"axis_adherence_verdict: {condition_matrix_result.judgment.axis_adherence_verdict}")
            if condition_matrix_result.judgment.axis_adherence_reason:
                print(condition_matrix_result.judgment.axis_adherence_reason)

    return result


# ======================
# 7) CLI
# ======================

def main():
    argv = sys.argv[1:]
    global PROBLEMS

    # Bootstrap parse for dynamic problem choices
    boot = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    boot.add_argument("--problems", type=str, default=None, help="Optional: JSON file to add/override problems")
    boot.add_argument("--problems-mode", choices=["merge", "replace"], default="merge",
                      help="merge: add/override built-ins (default) / replace: use only file problems")
    boot.add_argument("--list-problems", action="store_true", help="List available problem IDs and exit")
    boot_args, _ = boot.parse_known_args(argv)

    problems: Dict[str, Any] = dict(PROBLEMS)
    if boot_args.problems:
        loaded = load_problems_file(boot_args.problems)
        problems = loaded if boot_args.problems_mode == "replace" else {**problems, **loaded}

    if not problems:
        raise RuntimeError("No problems available.")

    if boot_args.list_problems:
        for pid in list(problems.keys()):
            print(pid)
        return

    default_problem = "donut_hole" if "donut_hole" in problems else next(iter(problems.keys()))

    ap = argparse.ArgumentParser(allow_abbrev=False)

    ap.add_argument("--problems", type=str, default=boot_args.problems, help="Optional: JSON file to add/override problems")
    ap.add_argument("--problems-mode", choices=["merge", "replace"], default=boot_args.problems_mode,
                    help="merge: add/override built-ins (default) / replace: use only file problems")
    ap.add_argument("--list-problems", action="store_true", help="List available problem IDs and exit")

    ap.add_argument("--problem", type=str, default=default_problem, choices=list(problems.keys()))
    ap.add_argument("--provider", type=str, required=True, choices=["openai", "anthropic", "mistral", "google", "hf"])
    ap.add_argument("--model", type=str, required=True, help="Model name (API) or local HF path/repo-id (hf)")
    ap.add_argument("--api-key", type=str, default=None, help="Optional: override env var for the provider")
    ap.add_argument("--answer-mode", choices=list(ANSWER_MODES), default="diagram_only",
                    help="diagram_only (default) or diagram_plus_tags")
    ap.add_argument("--prompt-priority", choices=list(PROMPT_PRIORITIES), default="balanced",
                    help="balanced (default) or method_first to prioritize diagram-mediated reasoning over task completion")
    ap.add_argument("--seed", type=int, default=None, help="Optional: seed for reproducible corruption RNG and local randomness")

    # 温度設計
    ap.add_argument("--temperature", type=float, default=0.7, help="Phase A temperature (ASCII thinking)")
    ap.add_argument("--answer-temperature", type=float, default=None, help="Phase B/C temperature (default: same as --temperature)")
    ap.add_argument("--test-temperature", type=float, default=0.0, help="Test temperature")

    ap.add_argument("--max-output-tokens", type=int, default=900)

    # OpenAI extras
    ap.add_argument("--openai-base-url", type=str, default=None, help="Optional: custom base_url for OpenAI-compatible endpoints")
    ap.add_argument("--openai-reasoning-effort", choices=["low", "medium", "high"], default=None,
                    help="Optional: OpenAI reasoning effort for reasoning-capable models")

    # Google extras
    ap.add_argument("--google-retry-429", type=int, default=2, help="Retry count for 429 errors (google)")
    ap.add_argument("--google-debug-empty", action="store_true", help="Print debug dumps when google returns empty text")

    # HF extras
    ap.add_argument("--offline", action="store_true", help="HF: force offline (HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE + local_files_only)")
    ap.add_argument("--hf-local-files-only", action="store_true", help="HF: local_files_only=True")
    ap.add_argument("--hf-cache-dir", type=str, default=None, help="HF: cache_dir (optional)")
    ap.add_argument("--hf-offload-folder", type=str, default="offload", help="HF: offload_folder for device_map='auto' disk offload")
    ap.add_argument("--hf-device", type=str, default="auto", help="auto/cpu/cuda/mps ...")
    ap.add_argument("--hf-dtype", type=str, default="auto", help="auto/float16/bfloat16/float32")
    ap.add_argument("--hf-trust-remote-code", action="store_true")
    ap.add_argument("--hf-load-in-8bit", action="store_true")
    ap.add_argument("--hf-load-in-4bit", action="store_true")
    ap.add_argument("--hf-disable-chat-template", action="store_true", help="HF: skip apply_chat_template, always use transcript fallback")

    # HF interpretability (field metrics)
    ap.add_argument("--field-metrics", action="store_true", help="HF: track field metrics during Phase B (adds overhead)")
    ap.add_argument("--field-window", type=int, default=128, help="Token window for field metrics (<=0 means all tokens)")
    ap.add_argument("--field-time-layer", type=str, default="last", help="Layer for token-time evolution (int or 'last')")
    ap.add_argument("--field-time-every", type=int, default=5, help="Record token-time metrics every N tokens")
    ap.add_argument("--field-fit-k", type=int, default=64, help="Max eigenvalues for power-law fit (curvature)")
    ap.add_argument("--field-layers", type=str, default="all", help="Layers to track (all|last|comma-separated indices)")

    # output/logging
    ap.add_argument("--save", type=str, default=None, help="Dir to save diagram+json logs")
    ap.add_argument("--print-diagram", action="store_true", help="Print DIAGRAM to stdout (debug)")

    # Phase A validation/retry
    ap.add_argument("--phase-a-max-attempts", type=int, default=3, help="Max attempts for Phase A (validate/retry)")
    ap.add_argument("--phase-a-min-tags", type=int, default=1, help="Min number of valid TAGS required in Phase A")
    ap.add_argument("--phase-a-axis-binding", action="store_true",
                    help="In method_first, ask Phase A to bind distinct query analysis axes to distinct local motif families without labels")
    ap.add_argument("--allow-invalid-phase-a", action="store_true",
                    help="Proceed even if Phase A still violates validation after retries (debug/analysis only)")

    # DIAGRAM内の英字ラベル例外（旧挙動）
    g3 = ap.add_mutually_exclusive_group()
    g3.add_argument("--allow-tag-label-exception", action="store_true",
                    help="Allow TAG labels like object_a inside DIAGRAM (legacy behavior)")
    g3.add_argument("--no-tag-label-exception", action="store_true",
                    help="Force strict symbols-only DIAGRAM (default)")

    # tests
    ap.add_argument("--run-tests", action="store_true", help="Run tests")
    ap.add_argument("--run-condition-matrix", action="store_true",
                    help="Run the higher-level condition matrix")
    ap.add_argument("--condition-matrix-conditions", type=str, default=",".join(CONDITION_MATRIX_DEFAULT_CONDITIONS),
                    help="Comma-separated subset of: no_query_strict,no_query_with_axis,equiv_diagram,cross_diagram; no_query aliases to no_query_strict")
    ap.add_argument("--condition-compare-mode", choices=list(CONDITION_COMPARE_MODES), default="semantic_llm",
                    help="Comparison mode for condition matrix: semantic_llm (default), hybrid, or surface")
    ap.add_argument("--equiv-diagram-mode", choices=list(EQUIV_DIAGRAM_MODES), default="vertical_flip_remap",
                    help="How to generate a topology-preserving diagram appearance transform for the condition matrix")
    ap.add_argument("--cross-diagram-source-mode", choices=["auto", "bank_only", "saved_only"], default="auto",
                    help="Cross-diagram source policy: auto=bank first then saved, bank_only, or saved_only")
    ap.add_argument("--cross-problem", type=str, default=None,
                    help="Optional source problem id for cross_diagram (defaults to a deterministic other-problem choice)")
    ap.add_argument("--condition-pass-threshold", type=float, default=0.55,
                    help="Similarity threshold for treating a condition-matrix row as surviving")
    ap.add_argument("--condition-soft-threshold", type=float, default=0.35,
                    help="Lower similarity threshold for borderline condition-matrix rows")
    ap.add_argument("--test-mode", choices=["lite", "full"], default=None,
                    help="lite: ablation + tamper_both / full: ablation + remove/add/both")
    ap.add_argument("--skip-caption", action="store_true", help="Skip Phase C to reduce calls")
    ap.add_argument("--tamper-remove", type=str, default="gap")
    ap.add_argument("--tamper-add", type=str, default="proxy")

    # fallback tags
    ap.add_argument("--no-fallback-tags", action="store_true", help="Disable fallback_tags injection when TAGS is empty")

    # contrib tests (2x2)
    g1 = ap.add_mutually_exclusive_group()
    g1.add_argument("--contrib-tests", action="store_true", help="Enable 2x2 contribution tests (adds calls)")
    g1.add_argument("--no-contrib-tests", action="store_true", help="Disable 2x2 contribution tests")

    # diagram tests (corrupt/swap)
    g2 = ap.add_mutually_exclusive_group()
    g2.add_argument("--diagram-tests", action="store_true", help="Enable diagram corruption/swap tests (adds calls)")
    g2.add_argument("--no-diagram-tests", action="store_true", help="Disable diagram corruption/swap tests")

    ap.add_argument("--diagram-corrupt-mode", choices=["noise", "shuffle_lines", "drop_lines"], default="noise")
    ap.add_argument("--diagram-corrupt-rate", type=float, default=0.12)
    ap.add_argument("--diagram-swap-mode", choices=["auto", "bank_only", "saved_only"], default="auto",
                    help="Swap source policy: auto=bank first then saved, bank_only, or saved_only")
    ap.add_argument("--swap-bank", type=str, default=None,
                    help="Optional JSON file for controlled adversarial swap diagrams (default: built-in bank if present)")

    args = ap.parse_args(argv)

    # Apply (possibly different) problems file after full parse
    problems2: Dict[str, Any] = dict(PROBLEMS)
    if args.problems:
        loaded2 = load_problems_file(args.problems)
        problems2 = loaded2 if args.problems_mode == "replace" else {**problems2, **loaded2}
    PROBLEMS = problems2

    llm = make_llm(args)
    save_dir = Path(args.save) if args.save else None

    # test-mode デフォルト:
    if args.test_mode is None:
        test_mode = "lite" if (args.provider == "google" and args.run_tests) else "full"
    else:
        test_mode = args.test_mode

    # answer-temperature デフォルト:
    answer_temp = args.temperature if args.answer_temperature is None else args.answer_temperature

    # contrib_tests デフォルト:
    if args.contrib_tests:
        contrib_tests = True
    elif args.no_contrib_tests:
        contrib_tests = False
    else:
        # google+lite は無料枠で厳しめなのでOFF推奨
        contrib_tests = not (args.provider == "google" and args.run_tests and test_mode == "lite")

    # diagram_tests デフォルト:
    if args.diagram_tests:
        diagram_tests = True
    elif args.no_diagram_tests:
        diagram_tests = False
    else:
        # google はコスト/クォータが重いのでデフォOFF
        diagram_tests = not (args.provider == "google" and args.run_tests)

    allow_tag_label_exception = bool(args.allow_tag_label_exception)
    condition_matrix_conditions = parse_condition_matrix_conditions(args.condition_matrix_conditions)
    condition_compare_mode = parse_condition_compare_mode(args.condition_compare_mode)

    run_once(
        llm=llm,
        provider=args.provider,
        model=args.model,
        problem_id=args.problem,
        answer_mode=args.answer_mode,
        prompt_priority=args.prompt_priority,
        run_seed=args.seed,
        field_metrics=args.field_metrics,
        field_window=args.field_window,
        field_time_layer=args.field_time_layer,
        field_time_every=args.field_time_every,
        field_fit_k=args.field_fit_k,
        field_layers=args.field_layers,
        save_dir=save_dir,
        print_diagram=args.print_diagram,
        run_tests=args.run_tests,
        run_condition_matrix_suite=args.run_condition_matrix,
        condition_matrix_conditions=condition_matrix_conditions,
        condition_compare_mode=condition_compare_mode,
        equiv_diagram_mode=args.equiv_diagram_mode,
        cross_diagram_source_mode=args.cross_diagram_source_mode,
        cross_problem_id=args.cross_problem,
        condition_pass_threshold=args.condition_pass_threshold,
        condition_soft_threshold=args.condition_soft_threshold,
        test_mode=test_mode,
        contrib_tests=contrib_tests,
        diagram_tests=diagram_tests,
        diagram_corrupt_mode=args.diagram_corrupt_mode,
        diagram_corrupt_rate=args.diagram_corrupt_rate,
        diagram_swap_mode=args.diagram_swap_mode,
        swap_bank_path=args.swap_bank,
        skip_caption=args.skip_caption,
        enable_fallback_tags=(not args.no_fallback_tags),
        tamper_remove=args.tamper_remove,
        tamper_add=args.tamper_add,
        phase_a_max_attempts=args.phase_a_max_attempts,
        phase_a_min_tags=args.phase_a_min_tags,
        phase_a_axis_binding=args.phase_a_axis_binding,
        allow_tag_label_exception=allow_tag_label_exception,
        allow_invalid_phase_a=args.allow_invalid_phase_a,
        temperature_a=args.temperature,
        temperature_answer=answer_temp,
        temperature_test=args.test_temperature,
    )

if __name__ == "__main__":
    main()
