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
- Provider SDKs are optional; install only what you use:
  - OpenAI: `pip install openai`
  - Anthropic: `pip install anthropic`
  - Mistral: `pip install mistralai`
  - Google (Gemini): `pip install google-genai`
  - Local Hugging Face models: `pip install torch transformers accelerate` (optional: `bitsandbytes`)

## Quickstart

Run `--help` to see all options:

```bash
python3 ascii_thought_lab_multi.py --help
```

Minimal run (no tests):

```bash
python3 ascii_thought_lab_multi.py \
  --provider openai \
  --model <MODEL_NAME> \
  --problem donut_hole
```

Run with tests + save logs:

```bash
python3 ascii_thought_lab_multi.py \
  --provider anthropic \
  --model <MODEL_NAME> \
  --problem whatis_sunyata \
  --run-tests \
  --save runs/
```

Print the raw diagram (otherwise only the diagram hash is printed):

```bash
python3 ascii_thought_lab_multi.py \
  --provider mistral \
  --model <MODEL_NAME> \
  --problem philo_zombie \
  --print-diagram
```

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
python3 ascii_thought_lab_multi.py \
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

## Outputs

With `--save <DIR>`, each run writes:

- `<provider>_<problem>_<timestamp>.diagram.txt` (the raw diagram; local only)
- `<provider>_<problem>_<timestamp>.json` (all run metadata + answers + test results)

Saving multiple runs enables the **diagram swap** test (it can reuse a different saved diagram).

## Problems and tags

- Built-in problems live in `ascii_thought_lab_multi.py` under `PROBLEMS` (problem IDs are the CLI `--problem` choices).
- The allowed tag vocabulary is `TAG_VOCAB`. Unknown tags are dropped during parsing/validation.

The built-in prompts and problems are currently written in Japanese; feel free to translate/customize them for your experiments.

## License

MIT (see `LICENSE`).
