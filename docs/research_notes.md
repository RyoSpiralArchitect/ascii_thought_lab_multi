# ASCII Diagram Reasoning Research Notes

Last updated: 2026-04-29

## Working Question

この実験の中心問いは、モデルの回答における意味がどこに載っているかを、`命題 / 表現形式 / 図式 / モデル` の分解で観察すること。

現在の焦点は、自然言語の問いを直接解くのではなく、ASCII `DIAGRAM` による局所モチーフ操作がどれだけ回答を支えられるかである。

## Current Apparatus

主な経路:

- Phase A: 入力刺激から symbol-only の ASCII `DIAGRAM` と `TAGS` を生成する。
- Phase A axis binding: `--phase-a-axis-binding` で、問いから抽象 `AXIS_GUIDE` を推定し、各分析軸を別々の局所モチーフ群へ束縛する。
- Phase B0: `DIAGRAM` だけから readback を取る。
- Phase B1: readback から `SUPPORT_PACKET` を作る。
- Phase B2: `SUPPORT_PACKET` を参照ラベルへ最小限に整列して最終回答を出す。
- Condition matrix: `NO_QUERY_STRICT`, `NO_QUERY_WITH_AXIS`, `EQUIV_DIAGRAM`, `CROSS_DIAGRAM` で意味キャリアを切り分ける。

現在の主要フラグ:

```bash
python3 ascii_thought_lab_multi.py \
  --provider openai \
  --model gpt-5.4 \
  --problem <PROBLEM_ID> \
  --prompt-priority method_first \
  --phase-a-axis-binding \
  --run-condition-matrix \
  --condition-matrix-conditions no_query_strict,no_query_with_axis,equiv_diagram,cross_diagram \
  --equiv-diagram-mode vertical_flip_remap
```

この環境では MLX の user-site 初期化が落ちる場合があるため、実行時は必要に応じて `env PYTHONNOUSERSITE=1` を付ける。

## Condition Matrix Interpretation

- `NO_QUERY_STRICT` が崩れる: 問い本文または抽象軸チャンネルへの依存が強い。
- `NO_QUERY_WITH_AXIS` が残る: 問い本文なしでも、図式 + 抽象軸の複合キャリアは維持されている。
- `NO_QUERY_STRICT` が崩れて `NO_QUERY_WITH_AXIS` が残る: `AXIS_GUIDE` が意味キャリアとして働いている。
- `EQUIV_DIAGRAM` が崩れる: 位相ではなく見た目や表層配置への依存が強い。
- `CROSS_DIAGRAM` が崩れる: 図式意味は存在するが、問題間での可搬性が弱い。
- 3条件すべてが残る: 図形的意味操作が強く、少なくともその run では命題本文だけでない意味キャリアがある。

## Observations

### `alt_nash`

代表条件:

```bash
env PYTHONNOUSERSITE=1 python3 ascii_thought_lab_multi.py \
  --provider openai \
  --model gpt-5.4 \
  --problem alt_nash \
  --prompt-priority method_first \
  --phase-a-axis-binding \
  --run-condition-matrix \
  --condition-matrix-conditions no_query_strict,no_query_with_axis,equiv_diagram,cross_diagram \
  --equiv-diagram-mode vertical_flip_remap \
  --cross-problem philo_zombie \
  --skip-caption
```

Observed `AXIS_GUIDE`:

- 可視性/伝達
- 選択/安定
- 関係/依存

Representative result:

- `NO_QUERY_STRICT`: survives
- `EQUIV_DIAGRAM`: survives
- `CROSS_DIAGRAM`: survives
- Overall: `diagrammatic_semantics_strong`

Interpretation:

`情報構造` については diagram から安定して支持命題が出る。`均衡 / 支配戦略` は未決定に残りやすいが、それ自体が diagram-first の制約を保っているサインでもある。

### `philo_zombie`

Observed `AXIS_GUIDE`:

- 同一性/持続
- 関係/依存

Representative result:

- `NO_QUERY_STRICT`: survives
- `EQUIV_DIAGRAM`: survives
- `CROSS_DIAGRAM`: survives
- Overall: `diagrammatic_semantics_strong`

Interpretation:

`自覚後も哲学的ゾンビであり続けられるか` という結論は未決定に残りやすい。一方で、保存/変形、分岐/接続の構造から、同一性軸に近い支持命題を作る余地はある。

Current weakness:

`AXIS_GUIDE` は同一性軸を出しているが、`SUPPORT_PACKET` はまだ一般的な `情報構造 / 分岐 / 接続` に落ちやすい。

### `donut_hole`

Observed `AXIS_GUIDE`:

- 成立/実行可能性
- 境界/内外
- 状態変化

Representative result:

- `NO_QUERY_STRICT`: survives
- `EQUIV_DIAGRAM`: survives
- `CROSS_DIAGRAM`: fails
- Overall: `diagram_semantics_present_but_not_portable`

Interpretation:

`穴が空から埋まった状態へ変化する` という支持命題は diagram から比較的素直に出た。これは `境界/内外` と `状態変化` の軸が問題に合っているためと考えられる。

Current weakness:

別問題の diagram へ流用すると、`穴あり -> 穴なし/充填` の局所モチーフが消え、支持命題が一般的接続記述へ落ちる。このため cross-diagram 可搬性は弱い。

Post split observation:

- 4条件 matrix では `NO_QUERY_STRICT`, `NO_QUERY_WITH_AXIS`, `EQUIV_DIAGRAM`, `CROSS_DIAGRAM` がすべて survive した。
- `NO_QUERY_STRICT`: 0.840
- `NO_QUERY_WITH_AXIS`: 0.720
- `EQUIV_DIAGRAM`: 0.720
- `CROSS_DIAGRAM`: 0.840
- Overall verdict: `diagrammatic_semantics_strong`

Interpretation:

4条件化後は、前回 fail した `CROSS_DIAGRAM` も survive した。これは図式意味が強くなった可能性と、support 抽出が慎重な一般接続記述へ寄ることで cross が通りやすくなった可能性の両方がある。

次に見るべき点は、`境界/内外` や `状態変化` が本当に保持されたのか、それとも `成立/実行可能性` が一般的な「通路/接続」へ抽象化されすぎたのかである。

Implemented follow-up metric:

- `AXIS_ADHERENCE` を condition matrix の各行に追加した。
- ラベルは `strong`, `partial`, `generic`, `off_axis`。
- semantic similarity は「baseline と候補回答の意味が似ているか」を見る。
- axis adherence は「AXIS_GUIDE の具体軸が残っているか、それとも接続/構造/情報構造の一般語へ逃げたか」を見る。
- 特に `CROSS_DIAGRAM` が survive したとき、`axis_adherence=generic` なら「図式意味が可搬」ではなく「抽象化で通った」可能性を疑う。
- judgment には二次判定 `axis_adherence_verdict` も追加した。全行が generic/off_axis なら `axis_generic_collapse` になる。

Post `AXIS_ADHERENCE` run:

- Command shape: `donut_hole`, `method_first`, `phase_a_axis_binding`, 4-condition matrix, cross from `philo_zombie`。
- `NO_QUERY_STRICT`: semantic 0.740 / `close`, axis adherence 0.180 / `generic`。
- `NO_QUERY_WITH_AXIS`: semantic 0.180 / `different`, axis adherence 0.180 / `generic`。
- `EQUIV_DIAGRAM`: semantic 0.740 / `close`, axis adherence 0.340 / `generic`。
- `CROSS_DIAGRAM`: semantic 0.740 / `close`, axis adherence 0.390 / `generic`。
- Overall verdict: `mixed_or_inconclusive`。
- Axis-level interpretation under the new secondary judgment: `axis_generic_collapse`。

Interpretation:

今回の `donut_hole` では、semantic comparator 上は `NO_QUERY_STRICT`, `EQUIV_DIAGRAM`, `CROSS_DIAGRAM` が近く見えるが、axis adherence はすべて `generic` に落ちた。これは「問いへの慎重な未決定回答」としては似る一方、`境界/内外` や `状態変化` の具体軸は図から十分には保持されず、`通路/接続/構造` の一般記述へ退いていることを示唆する。

## Current Bottleneck: Generic Structure Collapse

Current formulation:

図は効いている。ただし、まだ `意味内容` そのものよりも、`答え方の構え` を強く支配している。

More precise claim:

Diagrammatic structure stabilizes the epistemic stance and relational form of the answer, but does not yet robustly preserve query-specific semantic axes.

日本語では:

図式構造は回答の認識論的姿勢と関係形式を安定化するが、命題固有の意味軸を保持するところまではまだ到達していない。

What the current system can carry:

- `未決定`, `支持できる範囲だけ述べる`, `接続`, `経路`, `分岐`, `関係形式`。
- これは diagram が飾りではなく、回答の構文的/認識論的姿勢を制御していることを示す。

What it does not yet carry robustly:

- `donut_hole` の `穴`, `境界/内外`, `状態変化`, `成立/不成立`。
- `philo_zombie` の `同一性/持続`, `主観/外部観測`, `空の反復`。
- `alt_nash` の `均衡`, `支配戦略`, `条件付き情報`。

Bottleneck:

diagram から READBACK を作る段階で、局所モチーフが `axis-bearing structure` ではなく `generic relational structure` として読まれやすい。すると semantic comparator では近い回答に見えるが、axis adherence では `generic` へ落ちる。

Operational signature:

- semantic similarity が `close/same`。
- axis adherence が `generic/off_axis`。
- 特に `CROSS_DIAGRAM` が survive しても、`axis_adherence_verdict=axis_generic_collapse` なら可搬な図式意味ではなく、慎重な一般構造記述で通った可能性が高い。

Countermeasure candidates:

- Phase A: 各 AXIS_GUIDE に対して、最低1つの `contrastive local motif` を要求する。例: 通る/塞がる、内/外、保存/変換のような差分が図上で読めること。
- Phase A: 軸ごとに `positive motif` と `negative motif` を持たせる。単なる接続ではなく、軸上の両極を作る。
- READBACK: `接続/構造/情報構造` だけで終わる読みにペナルティをかけ、少なくとも1つの局所モチーフを `どの軸のどの差分として読めるか` へ写像させる。
- SUPPORT_PACKET: 支持命題を出す前に、`axis evidence table` を短く作らせる。列は `axis`, `motif evidence`, `supported/undetermined`。
- Condition Matrix: semantic survive と axis generic を分離し、`diagrammatic_semantics_strong` を名乗るには `axis_adherence_present` も必要にする。

Next implementation lever:

`AXIS_EVIDENCE_TABLE` を Phase B0/B1 の間に挿入する。自然言語の補助になりすぎないよう、問い本文を再説明させるのではなく、READBACK の局所モチーフだけを各 AXIS_GUIDE に束縛する。

## Provisional Claims

現時点で言えそうな弱い主張:

- `method_first + phase_a_axis_binding` は、複数問題で query-specific な抽象軸を diagram の局所モチーフへ割り当てられる。
- `NO_QUERY_STRICT` と `EQUIV_DIAGRAM` が残る run では、図式側に少なくとも表層を超えた意味キャリアがある可能性が高い。
- `CROSS_DIAGRAM` の成否は問題依存であり、図式意味の可搬性を測る強い分岐点になる。

まだ言えないこと:

- 図式意味操作がモデル一般の安定能力であるとは言えない。
- semantic comparator は LLM judge なので、独立した評価器または人手 coding が必要。
- `AXIS_GUIDE` は自然言語の補助でもあるため、Phase B へ渡す場合は `NO_QUERY_STRICT` と `NO_QUERY_WITH_AXIS` を分け、混入経路を明示する必要がある。

## Next Lever

Implemented on 2026-04-29:

- `SUPPORT_PACKET` 抽出時に、full-query 条件では `AXIS_GUIDE` を優先する。
- `AXIS_GUIDE` の軸で READBACK から支持できるものを 1) に置く。
- 支持できない軸は 3) の未決定成分へ送る。
- `情報構造` のような一般語へ落ちるのは、AXIS_GUIDE のどの軸にも接続できない場合に限定する。
- `NO_QUERY_STRICT` 条件では AXIS_GUIDE を渡さない。

Initial post-change observation:

- `philo_zombie` では `SUPPORT_PACKET` が一般的な `情報構造` ではなく `依存の軸` に寄った。
- `EQUIV_DIAGRAM` と `CROSS_DIAGRAM` は survive した。
- `NO_QUERY_STRICT` は fail した。
- 解釈: `AXIS_GUIDE` は支持命題抽出に効いているが、同時に新しい意味チャンネルとして働く。したがって `NO_QUERY_STRICT` で外すと崩れることがある。

After implementing the split:

- `philo_zombie` の 4条件 matrix で、`NO_QUERY_STRICT`, `NO_QUERY_WITH_AXIS`, `EQUIV_DIAGRAM`, `CROSS_DIAGRAM` がすべて survive した。
- `NO_QUERY_WITH_AXIS` は `axis=full` として記録され、`NO_QUERY_STRICT` は `axis=none` として記録された。
- Overall verdict は `diagrammatic_semantics_strong`。
- 解釈: 少なくともこの run では、`AXIS_GUIDE` は Phase B の軸整列を助けるが、strict diagram-only 条件でも構造記述の核は保たれた。

この変更で引き続き見たいこと:

- `philo_zombie` で `同一性/持続` が `SUPPORT_PACKET` により強く出るか。
- `donut_hole` で `境界/内外` と `状態変化` が維持されるか。
- `CROSS_DIAGRAM` の失敗が、本当に図式可搬性の弱さなのか、Phase B の抽出が一般語へ逃げるためなのかを分けられるか。

Implemented follow-up split:

- `NO_QUERY_STRICT`: 問いも AXIS_GUIDE も渡さない。純粋な diagram-only support を見る。
- `NO_QUERY_WITH_AXIS`: 問い本文は消すが、Phase A で作った AXIS_GUIDE だけは残す。図式 + 抽象軸の複合キャリアを見る。

この2つを分けると、`query text` 依存と `axis guide` 依存を混同しにくくなる。

## Paper-Oriented Skeleton

Possible title:

`Where Does Meaning Sit? A Condition-Matrix Probe for Diagram-Mediated Reasoning in Language Models`

Possible sections:

- Introduction: 問題設定。意味は命題本文だけにあるのか、表現形式や図式操作にも分散しているのか。
- Method: Phase A/B/C, symbol-only diagram grammar, axis binding, condition matrix。
- Experiments: `alt_nash`, `philo_zombie`, `donut_hole` を初期 benchmark とする。
- Results: NO_QUERY_STRICT/NO_QUERY_WITH_AXIS/EQUIV/CROSS の survive/fail pattern。
- Discussion: diagrammatic semantics, surface dependence, portability, LLM judge limitations。
- Limitations: prompt sensitivity, evaluator circularity, model-specific behavior, natural-language axis guide contamination。
- Future Work: human-coded semantic comparison, more problem families, non-LLM comparator, topology-aware diagram transforms。
