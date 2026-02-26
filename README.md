# ASCII Thought Lab (Multi-Provider)

Single-file experiment runner for **“ASCII diagram reasoning”** prompts across multiple LLM providers.

It forces the model to think using an ASCII-only `[DIAGRAM]` plus a small fixed tag vocabulary (`[TAGS]`), then measures (roughly) how much the diagram/tags affect the final answer by running a set of ablation/tamper tests.

## What it does

The script runs in phases:

- **Phase A**: Generate `[SEED]`, an ASCII-only `[DIAGRAM]`, and `[TAGS]` (from a fixed vocabulary).
- **Phase B**: Answer the question using only the question + `DIAGRAM` + `TAGS`.
- **Phase C**: Produce a 1-line caption from `TAGS` (and the `DIAGRAM`).

Optional tests (`--run-tests`) re-run Phase B under different conditions and compute a quick similarity score (using `difflib.SequenceMatcher`) between the baseline answer and each variant:

- **Ablation**: `TAGS=[]` (NO_TAGS)
- **Tamper**: remove/add/both (auto-selects a tag if the requested one isn’t present)
- **Contribution (2x2)**: FULL / NO_DIAGRAM / NO_TAGS / NEITHER
- **Diagram tests**: corruption and (if possible) diagram swap from previous saved runs

Note: The similarity score is a lightweight heuristic, not a semantic evaluation.

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

Run with tests + save logs:

```bash
ascii-thought-lab-multi \
  --provider anthropic \
  --model <MODEL_NAME> \
  --problem whatis_sunyata \
  --run-tests \
  --save runs/
```

Print the raw diagram (otherwise only the diagram hash is printed):

```bash
ascii-thought-lab-multi \
  --provider mistral \
  --model <MODEL_NAME> \
  --problem philo_zombie \
  --print-diagram
```

## Reproducibility

- `--seed <INT>` controls the script-side RNG used by the **diagram corruption** test (and any other local randomness).
- The model-generated `[SEED]` printed in Phase A is separate and is *not* the RNG seed.

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

## Aggregate runs (CSV)

Convert saved run JSON files into a CSV:

```bash
ascii-thought-lab-multi-aggregate --in runs --out runs.csv
```

Add `--include-text` to include `caption_1line` and the full `answer` fields (can make the CSV large).

## Notes on `--run-tests` (cost / number of calls)

`--run-tests` makes multiple additional Phase B calls (ablation/tamper/2x2/corruption/swap), so expect a noticeable increase in latency and API usage.
To reduce calls:

- `--test-mode lite`
- `--no-contrib-tests`
- `--no-diagram-tests`
- `--skip-caption`

The diagram swap test also requires `--save` and at least one previously saved run.

## Warnings

The script prints `[WARN]` lines when answers remain *too similar* after removing tags/diagrams/corrupting diagrams, as a heuristic signal that the model may be ignoring the intended inputs.

## Problems and tags

- Built-in problems live in `ascii_thought_lab_multi.py` under `PROBLEMS` (problem IDs are the CLI `--problem` choices).
- The allowed tag vocabulary is `TAG_VOCAB`. Unknown tags are dropped during parsing/validation.

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
