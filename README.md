# ASCII Thought Lab (Multi-Provider)

Single-file experiment runner for **“ASCII diagram reasoning”** prompts across multiple LLM providers.

It forces the model to think using an ASCII-only `[DIAGRAM]`, then measures (roughly) how much the diagram affects the final answer by running ablation and corruption tests. A small fixed tag vocabulary (`[TAGS]`) is still available as an optional auxiliary channel, but the default answer path is now diagram-first.

## What it does

The script runs in phases:

- **Phase A**: Generate `[SEED]`, an ASCII-only `[DIAGRAM]`, and `[TAGS]` (from a fixed vocabulary).
- **Phase B**: In `method_first`, first do a `DIAGRAM`-only readback, then minimally align that readback to the reference label. In balanced mode, answer from the question + `DIAGRAM` as before.
- **Phase C**: Produce a 1-line summary. In `method_first`, this is a readback summary rather than a free caption.

You can opt back into the old behavior with `--answer-mode diagram_plus_tags`.
You can also opt back into the older label-heavy diagram style with `--allow-tag-label-exception`, but strict symbol-only diagrams are now the default.
The current default is a **soft grammar**: a small core of operator semantics is fixed, while the model is free to choose a local symbol inventory and reuse it consistently within a run.
You can also switch the instruction policy with `--prompt-priority method_first`, which explicitly treats the problem as material for diagram-mediated reasoning rather than the primary goal. In this mode, Phase B is split into:

- **B0**: `DIAGRAM`-only readback (`supported / unsupported / undetermined`)
- **B1**: extract at least one supported proposition from that readback
- **B2**: minimally align that supported proposition to the reference label without losing the remaining uncertainty

When you also pass `--phase-a-axis-binding` together with `--prompt-priority method_first`, Phase A is asked to bind distinct query analysis axes to distinct local motif families without using labels. The runner now derives a small abstract `AXIS_GUIDE` from the query itself (for example, visibility/transfer, identity/persistence, possibility/constraint, boundary/scope), so the same mechanism can be reused across different problems instead of only one hand-tuned task.

Research notes and paper-oriented observations are collected in [docs/research_notes.md](docs/research_notes.md).

Optional tests (`--run-tests`) re-run Phase B under different conditions and compute a quick similarity score (using `difflib.SequenceMatcher`) between the baseline answer and each variant:

- **Contribution (2x2)**: FULL / NO_DIAGRAM / NO_TAGS / NEITHER
- **Diagram tests**: corruption and controlled diagram swap from an adversarial bank (or from previous saved runs as fallback)
- **Tag tests**: `NO_TAGS` ablation and tamper remove/add/both, only when `--answer-mode diagram_plus_tags`

There is also an optional **condition matrix** (`--run-condition-matrix`) aimed at the next-stage meaning-carrier questions:

- `NO_QUERY_STRICT`: remove both the Phase B query text and the `AXIS_GUIDE`
- `NO_QUERY_WITH_AXIS`: remove the Phase B query text but keep the abstract `AXIS_GUIDE`
- `EQUIV_DIAGRAM`: apply a topology-preserving appearance transform to the baseline diagram
- `CROSS_DIAGRAM`: reuse a diagram from a different problem against the current query

The runner turns those rows into a lightweight verdict such as:

- `query_or_proposition_dependent`
- `surface_form_dependent`
- `diagram_semantics_present_but_not_portable`
- `diagrammatic_semantics_strong`
- or a mixed/borderline verdict when the split is not clean

By default the condition matrix now uses a semantic comparator (`--condition-compare-mode semantic_llm`) rather than raw string overlap, while still keeping the surface score in the saved JSON. When `phase_a_axis_guide` is available, each condition row also gets an `axis_adherence` score/label (`strong`, `partial`, `generic`, `off_axis`) to separate true axis retention from generic connection/structure descriptions. The judgment block also reports a secondary `axis_adherence_verdict` such as `axis_generic_collapse` when semantic similarity survives but concrete axes do not.

Note: The comparator and axis-adherence scores are lightweight LLM-judge probes, not final semantic ground truth.

Soft grammar defaults:

- `=>` / `->`: transformation, mapping, or state update
- `-` / `|` / `/` / `\`: relation, linkage, boundary, or separation
- nesting with `[]` / `()`: grouping, containment, or hierarchy
- the rest of the symbol inventory is flexible, but repeated motifs should keep the same local role

## Requirements

- Python 3.10+ recommended

## Install (recommended)

Install the dependencies you need via extras:

```bash
pip install -e ".[openai]"      # or: [anthropic], [mistral], [google], [hf], [all]
```

This also installs a CLI entry point:

```bash
ascii-thought-lab-multi --help
```

If you prefer running without installation, you can also invoke the script directly:

```bash
python3 ascii_thought_lab_multi.py --help
```

## Quickstart

Run `--help` to see all options:

```bash
ascii-thought-lab-multi --help
```

Minimal run (no tests):

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole
```

Method-first variant:

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole \
  --prompt-priority method_first
```

The saved JSON will also include `diagram_readback`, which is the B0 readback used by the final answer.
In `method_first`, the saved JSON also includes `diagram_support`, which is the intermediate support packet used before the final answer projection.
When `--phase-a-axis-binding` is enabled, the saved JSON also includes `phase_a_axis_guide`, which records the abstract axis guide that was injected into Phase A.

Method-first with Phase A axis binding:

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem alt_nash \
  --prompt-priority method_first \
  --phase-a-axis-binding
```

Explicitly use the old tag-assisted answer path:

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole \
  --answer-mode diagram_plus_tags
```

Explicitly allow vocabulary labels such as `object_a` inside Phase A diagrams (legacy behavior):

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole \
  --allow-tag-label-exception
```

Run with tests + save logs:

```bash
ascii-thought-lab-multi \
  --provider anthropic \
  --model <MODEL_NAME> \
  --problem whatis_sunyata \
  --run-tests \
  --save runs/
```

Run the higher-level condition matrix:

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole \
  --run-condition-matrix
```

Tune the matrix rows / thresholds:

```bash
ascii-thought-lab-multi \
  --provider openai \
  --model <MODEL_NAME> \
  --problem alt_nash \
  --run-condition-matrix \
  --condition-matrix-conditions no_query_strict,no_query_with_axis,equiv_diagram,cross_diagram \
  --equiv-diagram-mode vertical_flip_remap \
  --cross-problem philo_zombie \
  --condition-pass-threshold 0.55 \
  --condition-soft-threshold 0.35
```

Print the raw diagram (otherwise only the diagram hash is printed):

```bash
ascii-thought-lab-multi \
  --provider mistral \
  --model <MODEL_NAME> \
  --problem philo_zombie \
  --print-diagram
```

Batch family sweep helper for Mistral:

```bash
bash scripts/run_mistral_family_baseline.sh
```

Both family sweep helpers currently default to `PROMPT_PRIORITY=method_first`.
They also default to `PHASE_A_MAX_ATTEMPTS=5`.

## Reproducibility

- `--seed <INT>` controls the script-side RNG used by the **diagram corruption** test (and any other local randomness).
- The model-generated `[SEED]` printed in Phase A is separate and is *not* the RNG seed.
- By default, a run now fails if Phase A still violates diagram validation after retries. Use `--allow-invalid-phase-a` only when you explicitly want to inspect dirty outputs.
- When such a failure happens and `--save` is set, the runner now saves `*_phase_a_failure.meta.txt`, `*.diagram.txt`, and one `*.attemptN.raw.txt` file per Phase A attempt for debugging.

## API keys

You can pass a key explicitly with `--api-key`, or rely on the provider SDK’s default environment variables.
Common env vars:

- OpenAI: `OPENAI_API_KEY`
- Anthropic: `ANTHROPIC_API_KEY`
- Mistral: `MISTRAL_API_KEY`
- Google: `GOOGLE_API_KEY`

## Local Hugging Face usage (provider: `hf`)

Example:

```bash
ascii-thought-lab-multi \
  --provider hf \
  --model <LOCAL_PATH_OR_REPO_ID> \
  --hf-device auto \
  --hf-dtype auto
```

Helpful flags:

- `--offline` (sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`)
- `--hf-local-files-only`
- `--hf-cache-dir <DIR>`
- `--hf-load-in-8bit` / `--hf-load-in-4bit`
- `--hf-disable-chat-template` (fallback to a simple transcript prompt)

## Field metrics (HF only)

When using `--provider hf`, you can enable an interpretability trace that records:

- **Layer-wise field trajectory**: `dim_eff`, `anisotropy`, `content_mass` (+ a curvature fit) per layer
- **Token-time field evolution**: the same metrics over generation time for a chosen layer
- **Curvature indicator**: power-law fit of the eigenvalue spectrum decay (`curvature_alpha`, `curvature_r2`)

Enable it (adds overhead):

```bash
ascii-thought-lab-multi \
  --provider hf \
  --model <LOCAL_PATH_OR_REPO_ID> \
  --problem donut_hole \
  --field-metrics \
  --field-window 128 \
  --field-time-layer last \
  --field-time-every 5
```

Notes:

- Metrics are computed over a **token window** (`--field-window`) of the generated token representations (centered covariance).
- Layer indices include the embedding output as layer `0`; `last` refers to the final layer output.
- Results are saved into the run JSON under `field_metrics` when `--save` is used.

## Outputs

With `--save <DIR>`, each run writes:

- `<provider>_<problem>_<timestamp>.diagram.txt` (the raw diagram; local only)
- `<provider>_<problem>_<timestamp>.json` (all run metadata + answers + test results)

Saving multiple runs enables the **diagram swap** test (it can reuse a different saved diagram).
Saved JSON also includes a `condition_matrix` block when `--run-condition-matrix` is used.

## Aggregate runs (CSV)

Convert saved run JSON files into a CSV:

```bash
ascii-thought-lab-multi-aggregate --in runs --out runs.csv
```

Add `--include-text` to include `caption_1line` and the full `answer` fields (can make the CSV large).
The aggregator now exports the main condition-matrix verdict plus the key similarity/status and axis-adherence columns for `NO_QUERY_STRICT`, `NO_QUERY_WITH_AXIS`, `EQUIV_DIAGRAM`, and `CROSS_DIAGRAM`.

## Controlled swap bank

By default, diagram swap now uses a built-in **adversarial swap bank** first:

- built-in file: `adversarial_swap_bank.json`
- default policy: `--diagram-swap-mode auto` = bank first, then saved diagrams as fallback
- force bank only: `--diagram-swap-mode bank_only`
- force legacy behavior: `--diagram-swap-mode saved_only`
- custom bank file: `--swap-bank path/to/bank.json`

Each bank entry is a symbol-only diagram crafted to be structurally plausible while nudging the reasoning in a different direction for the same benchmark problem.

## Notes on `--run-tests` (cost / number of calls)

`--run-tests` makes multiple additional Phase B calls (ablation/tamper/2x2/corruption/swap), so expect a noticeable increase in latency and API usage.
To reduce calls:

- `--test-mode lite`
- `--no-contrib-tests`
- `--no-diagram-tests`
- `--skip-caption`

The diagram swap test no longer requires `--save` if the built-in bank covers the problem. `--save` is only required when you want `saved_only` swap behavior or saved-diagram fallback.
When `--answer-mode diagram_only`, the tag ablation/tamper tests are skipped automatically because they are no longer informative.
`--run-condition-matrix` adds one Phase B call per requested condition and reuses `--test-temperature` for stability.
With the default semantic comparator, it also adds one short comparison call per condition-matrix row. If `phase_a_axis_guide` is present, it adds one additional short axis-adherence judge call per row.

## Warnings

The script prints `[WARN]` lines when answers remain *too similar* after removing tags/diagrams/corrupting diagrams, as a heuristic signal that the model may be ignoring the intended inputs.

## Problems and tags

- Built-in problems live in `ascii_thought_lab_multi.py` under `PROBLEMS` (problem IDs are the CLI `--problem` choices).
- The allowed tag vocabulary is `TAG_VOCAB`. Unknown tags are dropped during parsing/validation.
- In `diagram_only` mode, tags are still logged from Phase A, but they are not used to produce the final answer.
- By default, Phase A diagrams are now validated as symbol-only; English label tokens inside the diagram are rejected unless `--allow-tag-label-exception` is set.
- By default, Phase A also validates that the diagram contains both relation and transformation structure, plus at least two recurring motifs.

### Custom problems file

You can add/override problems from a JSON file:

```json
{
  "my_problem": "Write your question here...",
  "my_problem2": {
    "query": "Question text...",
    "fallback_tags": ["frame", "outside", "relation", "context", "void"],
    "tamper_remove": "frame",
    "tamper_add": "proxy"
  }
}
```

Usage:

```bash
ascii-thought-lab-multi --problems problems.json --problem my_problem
ascii-thought-lab-multi --problems problems.json --problems-mode replace --list-problems
```

The built-in prompts and problems are currently written in Japanese; feel free to translate/customize them for your experiments.

## License

MIT (see `LICENSE`).
