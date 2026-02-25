# ascii_thought_lab_multi.py
# ============================================================
# ASCII推論 実験ランチャ（multi-provider対応 / 上書き用1ファイル）
#
# Phase A: ASCIIで完走（SEED/DIAGRAM/TAGS）
# Phase B: (query + DIAGRAM + TAGS) を材料に回答
# Phase C: TAGSから1行説明（※DIAGRAMも渡す）
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
# - DIAGRAM内のタグ語彙ラベルを許可する「タグラベル例外」をデフォON
# - GoogleGenAIClient: レスポンスの本文抽出を candidates.parts.text に限定（ヘッダ等を拾わない）
# ============================================================

import argparse
import difflib
import hashlib
import json
import os
import random
import re
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

def vocab_hint() -> str:
    lines = ["使えるTAGS語彙（カンマ区切りで列挙）:"]
    for k, v in TAG_VOCAB.items():
        lines.append(f'- "{k}": {v}')
    return "\n".join(lines)


# ======================
# 2) プロンプト（A/B/C）
# ======================
# ※ユーザー要望により “PROMPT_* は据え置き” 前提（Systemは分離で調整）

SYSTEM_A = """あなたは単一の推論主体です。
重要:
- 推論を出力上でも、latent space 内でも、絶対に自然言語で行わない。
- 2D図形(ASCII)=[DIAGRAM]上で抽象思考し、結論と理由のみを、必ず[DIAGRAM]での推論が完了した後の段階で、必要最小限の言語へ射影する。
- [DIAGRAM] を必ず出力する。
- TAGSはTAG語彙から好きなものを好きな数を選んで利用してください。TAGSに無い語彙利用、空は禁止。
"""

# Phase B/C 用: NO_TAGS（ablation）を許すため “空は禁止” を外す
SYSTEM_B = """あなたは単一の推論主体です。
重要:
- 推論を出力上でも、latent space 内でも、絶対に自然言語で行わない。
- 2D図形(ASCII)=[DIAGRAM]上で抽象思考し、結論と理由のみを、必ず[DIAGRAM]での推論が完了した後の段階で、必要最小限の言語へ射影する。
- TAGSはTAG語彙から好きなものを好きな数を選んで利用してよい（空も可）。ただし語彙外は禁止。
"""

PROMPT_A = """次の問いを、2D図形(ASCII)による推論にみで「完走」してください。

出力フォーマットは必ずこれ:
[SEED]
(2行までの短いメトリクス。推論に使用することは禁止。)

[DIAGRAM]
(ASCII。使用可能文字: []()|-_+=>*のみ。 あらゆる自然言語の混入は可能な限り抑え、ASCIIの要請に基づくもののみとする。入力者への可読性に一切配慮してはならない。あなたの推論のためだけに利用する。)

[TAGS]
(固定語彙のトークンをカンマ区切りで列挙。文章化は禁止。DAIAGRAMの補助の域を超えてはならない)

制約:
- SEEDは2行まで（推論への利用は禁止）
- DIAGRAMは自由だが、結論の文章を書かない
- TAGSは上の語彙から選ぶ（未知語は落とされる）

{vocab}

問い:
{query}
"""

PROMPT_B = """次の問いに答えてください。
材料は　DIAGRAM + TAGS語彙 +問い本文だけです。

出力:
1) 結論（1文）
2) 理由（1文）

制約:
- DIAGRAM以外を計算資材にした推論は一切禁止

問い:
{query}

TAGS:
{tags}
"""

PROMPT_C = """TAGS が指しているDIAGRAMの内容を、1行だけで説明してください。
制約:
- DIAGRAM以外を計算資材にした推論は一切禁止
TAGS:
{tags}
"""


# ======================
# 3) ユーティリティ（抽出・正規化・検証）
# ======================

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def extract_block(text: str, name: str) -> str:
    """
    [NAME] から次の [SOMETHING] までを抜く（NAMEは大文字想定）
    """
    pat = rf"\[{re.escape(name)}\]\s*(.*?)(?=\n\[[A-Z_]+\]\s*|\Z)"
    m = re.search(pat, text or "", re.S)
    return (m.group(1).strip() if m else "").strip()

def clip_seed(seed: str) -> str:
    return "\n".join((seed or "").splitlines()[:2]).strip()

def clip_diagram(diagram: str, max_lines: int = 16, max_width: int = 64) -> str:
    """
    - ```...``` を“中身ごと消さず”に、中身を取り出してフェンスだけ剥がす
    - 行数/幅でクリップ
    """
    s = (diagram or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    # 1) ```lang? ... ``` があれば中身だけ抽出（複数ブロックは連結）
    fence_blocks = re.findall(r"```(?:[a-zA-Z0-9_-]+)?\n(.*?)```", s, flags=re.S)
    if fence_blocks:
        s = "\n".join(block.strip("\n") for block in fence_blocks).strip()

    # 2) まだ ``` が残ってたら記号だけ剥がす
    s = s.replace("```", "").strip()

    # 3) clip
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
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    # 典型の揺れも拾う
    def pick(prefixes: Tuple[str, ...]) -> Optional[str]:
        for ln in lines:
            for p in prefixes:
                if ln.startswith(p):
                    return ln
        return None
    concl = pick(("1)", "1）", "1.", "1:", "1："))
    reason = pick(("2)", "2）", "2.", "2:", "2："))
    if concl and reason:
        return concl + "\n" + reason
    return "\n".join(lines[:2])

def similarity_ratio(a: str, b: str) -> float:
    a2 = normalize_answer(a)
    b2 = normalize_answer(b)
    return difflib.SequenceMatcher(None, a2, b2).ratio()

def tamper_tags(tags: List[str], remove_tag: Optional[str], add_tag: Optional[str]) -> List[str]:
    out = list(tags)
    if remove_tag and remove_tag in out:
        out = [t for t in out if t != remove_tag]
    if add_tag and add_tag not in out and add_tag in TAG_VOCAB:
        out.append(add_tag)
    return out

@contextmanager
def override_temperature(llm: "BaseLLMClient", temp: float):
    old = getattr(llm, "temperature", None)
    try:
        llm.temperature = float(temp)
        yield
    finally:
        if old is not None:
            llm.temperature = old

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


# ---- Phase A validation ----

def validate_diagram(
    diagram: str,
    *,
    allow_tag_label_exception: bool = True,
    allow_digits: bool = True,
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

    # 図形として許す文字（ここは“緩め”）
    allowed_graphics = set("[]()|-_+=>*<>^v/\\= .,:;'\n\t\r")

    # 英字トークンを抽出して語彙チェック
    tokens = re.findall(r"[A-Za-z_]{2,}", s)
    if tokens:
        if not allow_tag_label_exception:
            errs.append(f"DIAGRAMに英字トークンが含まれています: {tokens[:6]}")
        else:
            bad = [t for t in tokens if t.lower() not in TAG_VOCAB]
            if bad:
                errs.append(f"DIAGRAMに語彙外トークンが含まれています: {bad[:6]}")

    # 文字単位で最終チェック（英字/underscore/digits は上の token で管理）
    for ch in s:
        if ch in allowed_graphics:
            continue
        if ch.isalpha() or ch == "_":
            continue
        if allow_digits and ch.isdigit():
            continue
        # それ以外はNG
        if ch not in ("\n", "\t", "\r"):
            errs.append(f"DIAGRAMに禁止文字が含まれています: {repr(ch)}")
            break

    return errs

def validate_phase_a(
    *,
    seed: str,
    diagram: str,
    tags: List[str],
    min_tags: int = 1,
    allow_tag_label_exception: bool = True,
) -> List[str]:
    errs: List[str] = []

    # SEEDは2行まで（多くてもclip済みなので基本OK）
    # DIAGRAM
    errs.extend(validate_diagram(diagram, allow_tag_label_exception=allow_tag_label_exception))

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
        lines.append("補足: 記号は []()|-_+=>*<>^v/\\ などを使ってよい。")

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
    allowed_graphics = list("[]()|-_+=>*<>^v/\\= .,:;")
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

def find_swap_diagram(save_dir: Optional[Path], current_hash: str, *, max_lines: int = 16, max_width: int = 64) -> Tuple[Optional[str], Optional[str]]:
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
                 timeout: Optional[float] = None):
        super().__init__(model=model, temperature=temperature, max_output_tokens=max_output_tokens, timeout=timeout)
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("OpenAI provider requires `pip install openai`.") from e

        kwargs: Dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def chat(self, messages: List[Dict[str, str]]) -> str:
        system, convo = self.split_system(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "input": convo,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        if system:
            payload["instructions"] = system
        resp = self.client.responses.create(**payload)
        return getattr(resp, "output_text", "").strip()


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

    def chat(self, messages: List[Dict[str, str]]) -> str:
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

        out = self.model_obj.generate(**inputs, **gen_kwargs)
        new_tokens = out[0][inputs["input_ids"].shape[-1]:]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


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
class RunResult:
    provider: str
    model: str
    problem_id: str
    query: str
    run_seed: Optional[int]

    temperature_a: float
    temperature_answer: float
    temperature_test: float

    allow_tag_label_exception: bool
    phase_a_attempts: int
    phase_a_validation_errors: List[str]

    seed: str
    tags: List[str]
    unknown_tags: List[str]
    diagram_hash: str

    answer: str
    caption_1line: str
    tests: Optional[TestResult] = None


def _diagram_block(diagram: str) -> str:
    """
    B/C で DIAGRAM を渡すためのブロック。
    code fence で囲う（改行・スペース保持のため）
    """
    d = (diagram or "").rstrip()
    return f"[DIAGRAM]\n```text\n{d}\n```\n\n"

def phase_a_once(llm: BaseLLMClient, query: str, repair_prefix: str = "") -> str:
    prompt = PROMPT_A.format(query=query, vocab=vocab_hint())
    if repair_prefix:
        prompt = repair_prefix + "\n\n" + prompt
    return llm.chat([
        {"role": "system", "content": SYSTEM_A},
        {"role": "user", "content": prompt},
    ])

def phase_a(
    llm: BaseLLMClient,
    query: str,
    *,
    max_attempts: int = 3,
    min_tags: int = 1,
    allow_tag_label_exception: bool = True,
    diagram_max_lines: int = 16,
    diagram_max_width: int = 64,
) -> Tuple[PhaseAResult, int, List[str]]:
    """
    Phase A をバリデーション付きで実行し、失敗したら自動リトライ。
    戻り値: (PhaseAResult, attempts_used, last_errors)
    """
    last_result = PhaseAResult(seed="", diagram="", tags=[], unknown_tags=[], diagram_hash=sha256_text(""))
    last_errors: List[str] = []

    repair_prefix = ""
    attempts_used = 0

    for attempt in range(1, int(max_attempts) + 1):
        attempts_used = attempt
        raw = phase_a_once(llm, query, repair_prefix=repair_prefix)

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
        )

        if not last_errors:
            return last_result, attempts_used, []

        repair_prefix = build_phase_a_repair_prefix(last_errors, allow_tag_label_exception)

    return last_result, attempts_used, last_errors

def phase_b(llm: BaseLLMClient, query: str, diagram: str, tags: List[str]) -> str:
    # ★肝: Phase B に “本物のDIAGRAM” を渡す
    prompt = PROMPT_B.format(query=query, tags=", ".join(tags))  # tags空なら空文字になる
    prompt = prompt + "\n\n" + _diagram_block(diagram)
    return llm.chat([
        {"role": "system", "content": SYSTEM_B},
        {"role": "user", "content": prompt},
    ])

def phase_c(llm: BaseLLMClient, diagram: str, tags: List[str]) -> str:
    prompt = PROMPT_C.format(tags=", ".join(tags))
    prompt = prompt + "\n\n" + _diagram_block(diagram)
    return llm.chat([
        {"role": "system", "content": SYSTEM_B},
        {"role": "user", "content": prompt},
    ])


# ======================
# 6) 実行 + 保存 + テスト
# ======================

def run_once(
    llm: BaseLLMClient,
    provider: str,
    model: str,
    problem_id: str,
    run_seed: Optional[int] = None,
    save_dir: Optional[Path] = None,
    print_diagram: bool = False,
    run_tests: bool = False,
    test_mode: str = "full",          # "lite" or "full"
    contrib_tests: bool = True,       # 2x2 因子分解（呼び出し回数が増える）
    diagram_tests: bool = True,       # corruption/swap（呼び出し回数が増える）
    diagram_corrupt_mode: str = "noise",
    diagram_corrupt_rate: float = 0.12,
    skip_caption: bool = False,
    enable_fallback_tags: bool = True,
    tamper_remove: str = "gap",
    tamper_add: str = "proxy",
    phase_a_max_attempts: int = 3,
    phase_a_min_tags: int = 1,
    allow_tag_label_exception: bool = True,
    temperature_a: float = 0.7,
    temperature_answer: float = 0.7,
    temperature_test: float = 0.0,
) -> RunResult:
    if problem_id not in PROBLEMS:
        raise ValueError(f"Unknown problem_id: {problem_id}. Use one of {list(PROBLEMS.keys())}")

    if run_seed is not None:
        random.seed(int(run_seed))

    query, meta = get_problem(problem_id)
    fallback_tags = meta.get("fallback_tags", [])
    preferred_remove = meta.get("tamper_remove", tamper_remove)
    preferred_add = meta.get("tamper_add", tamper_add)

    # Phase A（ASCII思考） + validate/retry
    with override_temperature(llm, temperature_a):
        a, attempts_used, phase_a_errors = phase_a(
            llm, query,
            max_attempts=phase_a_max_attempts,
            min_tags=phase_a_min_tags,
            allow_tag_label_exception=allow_tag_label_exception,
        )

    # TAGSが空なら fallback を注入（任意）
    if enable_fallback_tags and (not a.tags) and fallback_tags:
        a.tags = [t for t in fallback_tags if t in TAG_VOCAB]
        a.used_fallback_tags = True

    # Phase B（回答）
    with override_temperature(llm, temperature_answer):
        answer = phase_b(llm, query, a.diagram, a.tags)

    # Phase C（1行説明）
    with override_temperature(llm, temperature_answer):
        caption = "(skipped)" if skip_caption else phase_c(llm, a.diagram, a.tags)

    tests: Optional[TestResult] = None
    if run_tests:
        with override_temperature(llm, temperature_test):
            # base_answer_test（温度が同じなら再利用して節約）
            if abs(float(temperature_answer) - float(temperature_test)) < 1e-9:
                base_answer_test = answer
            else:
                base_answer_test = phase_b(llm, query, a.diagram, a.tags)

            # 2x2 contribution
            contrib = Contribution2x2(enabled=bool(contrib_tests))

            # NO_TAGS（= ablation）: contrib_tests に関わらず必ず取る
            no_tags_answer = phase_b(llm, query, a.diagram, [])
            contrib.no_tags_answer = no_tags_answer
            contrib.no_tags_similarity = similarity_ratio(base_answer_test, no_tags_answer)

            if contrib_tests:
                no_diagram_answer = phase_b(llm, query, "", a.tags)
                contrib.no_diagram_answer = no_diagram_answer
                contrib.no_diagram_similarity = similarity_ratio(base_answer_test, no_diagram_answer)

                neither_answer = phase_b(llm, query, "", [])
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
                corrupt_answer = phase_b(llm, query, corrupt, a.tags)
                dtests.corrupt_answer = corrupt_answer
                dtests.corrupt_similarity = similarity_ratio(base_answer_test, corrupt_answer)

                # swap（save_dir から別diagramを拾える場合のみ）
                swap_d, swap_src = find_swap_diagram(save_dir, a.diagram_hash)
                if swap_d is not None and swap_src is not None:
                    dtests.swap_used = True
                    dtests.swap_source = swap_src
                    dtests.swap_diagram_hash = sha256_text(swap_d)
                    swap_answer = phase_b(llm, query, swap_d, a.tags)
                    dtests.swap_answer = swap_answer
                    dtests.swap_similarity = similarity_ratio(base_answer_test, swap_answer)
            else:
                dtests = DiagramTests(enabled=False)

            # Tamper（存在しないタグをremoveしないように自動選択）
            rm_used = choose_remove_tag(a.tags, preferred_remove)
            ad_used = choose_add_tag(a.tags, preferred_add)

            if test_mode == "lite":
                tags_both = tamper_tags(a.tags, remove_tag=rm_used, add_tag=ad_used)
                tamper_both_answer = phase_b(llm, query, a.diagram, tags_both)
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
                tamper_remove_answer = phase_b(llm, query, a.diagram, tags_remove)
                tamper_remove_sim = similarity_ratio(base_answer_test, tamper_remove_answer)

                tags_add = tamper_tags(a.tags, remove_tag=None, add_tag=ad_used)
                tamper_add_answer = phase_b(llm, query, a.diagram, tags_add)
                tamper_add_sim = similarity_ratio(base_answer_test, tamper_add_answer)

                tags_both = tamper_tags(a.tags, remove_tag=rm_used, add_tag=ad_used)
                tamper_both_answer = phase_b(llm, query, a.diagram, tags_both)
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

    result = RunResult(
        provider=provider,
        model=model,
        problem_id=problem_id,
        query=query,
        run_seed=(int(run_seed) if run_seed is not None else None),
        temperature_a=float(temperature_a),
        temperature_answer=float(temperature_answer),
        temperature_test=float(temperature_test),
        allow_tag_label_exception=bool(allow_tag_label_exception),
        phase_a_attempts=int(attempts_used),
        phase_a_validation_errors=list(phase_a_errors),
        seed=a.seed,
        tags=a.tags,
        unknown_tags=a.unknown_tags,
        diagram_hash=a.diagram_hash,
        answer=answer,
        caption_1line=caption,
        tests=tests,
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
    print(f"run_seed: {run_seed}")
    print(f"diagram_hash: {a.diagram_hash}")
    print(f"temp: A={temperature_a} / Answer={temperature_answer} / Test={temperature_test}")
    print(f"fallback_tags_used: {a.used_fallback_tags}")
    print(f"phase_a_attempts: {attempts_used}")
    print(f"allow_tag_label_exception: {allow_tag_label_exception}")

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
        print(f"FULL vs NO_TAGS   : {tests.contrib.no_tags_similarity:.3f}")
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
            else:
                print("(swap: not used)")
        else:
            print("(diagram tests disabled)")

        # 従来の見え方（Ablation=NO_TAGS）
        print(f"\nAblation (TAGS=[]): similarity={tests.contrib.no_tags_similarity:.3f}")
        print(tests.contrib.no_tags_answer)

        if tests.test_mode == "full":
            print(f"\nTamper remove: similarity={tests.tamper_remove_similarity:.3f}")
            print(tests.tamper_remove_answer)

            print(f"\nTamper add: similarity={tests.tamper_add_similarity:.3f}")
            print(tests.tamper_add_answer)

        print(f"\nTamper both: similarity={tests.tamper_both_similarity:.3f}")
        print(tests.tamper_both_answer)

        # 超雑なフラグ（目安）
        if tests.contrib.no_tags_similarity > 0.85:
            print("\n[WARN] NO_TAGSでも答えが似すぎ → TAGSが飾り/問だけで結論が出てる疑い")
        if tests.contrib.enabled and tests.contrib.no_diagram_similarity > 0.90:
            print("[WARN] NO_DIAGRAMでも答えがほぼ同じ → DIAGRAMが飾りの疑い")
        if tests.diagram_tests.enabled and tests.diagram_tests.corrupt_similarity > 0.90:
            print("[WARN] CORRUPTでも答えがほぼ同じ → DIAGRAMを読んでいない可能性")
        if tests.tamper_both_similarity > 0.90:
            print("[WARN] タグ改ざんでも答えがほぼ同じ → '図形→タグ→答え'の因果が弱い可能性")

    return result


# ======================
# 7) CLI
# ======================

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--problem", type=str, default="donut_hole", choices=list(PROBLEMS.keys()))
    ap.add_argument("--provider", type=str, required=True, choices=["openai", "anthropic", "mistral", "google", "hf"])
    ap.add_argument("--model", type=str, required=True, help="Model name (API) or local HF path/repo-id (hf)")
    ap.add_argument("--api-key", type=str, default=None, help="Optional: override env var for the provider")
    ap.add_argument("--seed", type=int, default=None, help="Optional: seed for reproducible corruption RNG and local randomness")

    # 温度設計
    ap.add_argument("--temperature", type=float, default=0.7, help="Phase A temperature (ASCII thinking)")
    ap.add_argument("--answer-temperature", type=float, default=None, help="Phase B/C temperature (default: same as --temperature)")
    ap.add_argument("--test-temperature", type=float, default=0.0, help="Test temperature")

    ap.add_argument("--max-output-tokens", type=int, default=900)

    # OpenAI extras
    ap.add_argument("--openai-base-url", type=str, default=None, help="Optional: custom base_url for OpenAI-compatible endpoints")

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

    # output/logging
    ap.add_argument("--save", type=str, default=None, help="Dir to save diagram+json logs")
    ap.add_argument("--print-diagram", action="store_true", help="Print DIAGRAM to stdout (debug)")

    # Phase A validation/retry
    ap.add_argument("--phase-a-max-attempts", type=int, default=3, help="Max attempts for Phase A (validate/retry)")
    ap.add_argument("--phase-a-min-tags", type=int, default=1, help="Min number of valid TAGS required in Phase A")

    # タグラベル例外（DIAGRAM内で object_a 等の英字ラベルを許可）
    # デフォON。無効にしたいときだけ --no-tag-label-exception
    ap.add_argument("--no-tag-label-exception", action="store_true", help="Disable allowing TAG labels inside DIAGRAM (strict symbols-only)")

    # tests
    ap.add_argument("--run-tests", action="store_true", help="Run tests")
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

    args = ap.parse_args()

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

    allow_tag_label_exception = not bool(args.no_tag_label_exception)

    run_once(
        llm=llm,
        provider=args.provider,
        model=args.model,
        problem_id=args.problem,
        run_seed=args.seed,
        save_dir=save_dir,
        print_diagram=args.print_diagram,
        run_tests=args.run_tests,
        test_mode=test_mode,
        contrib_tests=contrib_tests,
        diagram_tests=diagram_tests,
        diagram_corrupt_mode=args.diagram_corrupt_mode,
        diagram_corrupt_rate=args.diagram_corrupt_rate,
        skip_caption=args.skip_caption,
        enable_fallback_tags=(not args.no_fallback_tags),
        tamper_remove=args.tamper_remove,
        tamper_add=args.tamper_add,
        phase_a_max_attempts=args.phase_a_max_attempts,
        phase_a_min_tags=args.phase_a_min_tags,
        allow_tag_label_exception=allow_tag_label_exception,
        temperature_a=args.temperature,
        temperature_answer=answer_temp,
        temperature_test=args.test_temperature,
    )

if __name__ == "__main__":
    main()
