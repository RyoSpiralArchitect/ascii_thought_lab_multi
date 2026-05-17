#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not set." >&2
  echo "Export it in the shell before running this script." >&2
  exit 1
fi

if command -v ascii-thought-lab-multi >/dev/null 2>&1; then
  RUNNER=(ascii-thought-lab-multi)
else
  RUNNER=(python3 ascii_thought_lab_multi.py)
fi

if command -v ascii-thought-lab-multi-aggregate >/dev/null 2>&1; then
  AGGREGATOR=(ascii-thought-lab-multi-aggregate)
else
  AGGREGATOR=(python3 ascii_thought_lab_aggregate.py)
fi

MODELS=(
  "${MODEL_1-gpt-5.4-2026-03-05}"
  "${MODEL_2-gpt-5-mini-2025-08-07}"
  "${MODEL_3-gpt-4.1-2025-04-14}"
)

PROBLEMS=(
  "${PROBLEM_1-donut_hole}"
  "${PROBLEM_2-philo_zombie}"
  "${PROBLEM_3-whatis_sunyata}"
  "${PROBLEM_4-alt_nash}"
)

FILTERED_MODELS=()
for model in "${MODELS[@]}"; do
  if [[ -n "$model" ]]; then
    FILTERED_MODELS+=("$model")
  fi
done
MODELS=("${FILTERED_MODELS[@]}")

FILTERED_PROBLEMS=()
for problem in "${PROBLEMS[@]}"; do
  if [[ -n "$problem" ]]; then
    FILTERED_PROBLEMS+=("$problem")
  fi
done
PROBLEMS=("${FILTERED_PROBLEMS[@]}")

if (( ${#MODELS[@]} == 0 )); then
  echo "No models configured." >&2
  exit 1
fi

if (( ${#PROBLEMS[@]} == 0 )); then
  echo "No problems configured." >&2
  exit 1
fi

STAMP="${STAMP:-$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-runs}"
RUN_DIR="${RUN_ROOT%/}/openai_gpt_baseline_${STAMP}"
CSV_PATH="${CSV_PATH:-${RUN_DIR}/summary.csv}"

TEMPERATURE="${TEMPERATURE:-0.7}"
ANSWER_TEMPERATURE="${ANSWER_TEMPERATURE:-0.2}"
TEST_TEMPERATURE="${TEST_TEMPERATURE:-0}"
SEED="${SEED:-42}"
TEST_MODE="${TEST_MODE:-lite}"
DRY_RUN="${DRY_RUN:-0}"
ANSWER_MODE="${ANSWER_MODE:-diagram_only}"
PROMPT_PRIORITY="${PROMPT_PRIORITY:-method_first}"
PHASE_A_MAX_ATTEMPTS="${PHASE_A_MAX_ATTEMPTS:-5}"
MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-900}"
GPT5_MINI_MAX_OUTPUT_TOKENS="${GPT5_MINI_MAX_OUTPUT_TOKENS:-2000}"
DIAGRAM_TESTS="${DIAGRAM_TESTS:-0}"
DIAGRAM_SWAP_MODE="${DIAGRAM_SWAP_MODE:-auto}"
SWAP_BANK="${SWAP_BANK:-}"

if [[ "$DIAGRAM_TESTS" == "1" ]]; then
  DIAGRAM_TEST_ARGS=(--diagram-tests)
else
  DIAGRAM_TEST_ARGS=(--no-diagram-tests)
fi

mkdir -p "$RUN_DIR"

FAILURES=()
TOTAL=0
ABORT_REASON=""

echo "run_dir=$RUN_DIR"
echo "models=${MODELS[*]}"
echo "problems=${PROBLEMS[*]}"
echo "answer_mode=$ANSWER_MODE prompt_priority=$PROMPT_PRIORITY phase_a_max_attempts=$PHASE_A_MAX_ATTEMPTS test_mode=$TEST_MODE diagram_swap_mode=$DIAGRAM_SWAP_MODE temperature=$TEMPERATURE answer_temperature=$ANSWER_TEMPERATURE test_temperature=$TEST_TEMPERATURE seed=$SEED"

for model in "${MODELS[@]}"; do
  for problem in "${PROBLEMS[@]}"; do
    TOTAL=$((TOTAL + 1))
    SAFE_MODEL="${model//\//_}"
    SAFE_PROBLEM="${problem//\//_}"
    RUN_LOG="${RUN_DIR}/$(printf '%02d' "$TOTAL")_${SAFE_MODEL}__${SAFE_PROBLEM}.log"
    RUN_MAX_OUTPUT_TOKENS="$MAX_OUTPUT_TOKENS"
    OPENAI_MODEL_EXTRA_ARGS=()
    if [[ "$model" == gpt-5-mini* ]]; then
      RUN_MAX_OUTPUT_TOKENS="$GPT5_MINI_MAX_OUTPUT_TOKENS"
      OPENAI_MODEL_EXTRA_ARGS+=(--openai-reasoning-effort low)
    fi
    echo
    echo "[$TOTAL] model=$model problem=$problem"
    echo "log=$RUN_LOG"

    CMD=(
      "${RUNNER[@]}"
      --provider openai
      --model "$model"
      --problem "$problem"
      --answer-mode "$ANSWER_MODE"
      --prompt-priority "$PROMPT_PRIORITY"
      --phase-a-max-attempts "$PHASE_A_MAX_ATTEMPTS"
      --run-tests
      --test-mode "$TEST_MODE"
      "${DIAGRAM_TEST_ARGS[@]}"
      --diagram-swap-mode "$DIAGRAM_SWAP_MODE"
      --skip-caption
      --temperature "$TEMPERATURE"
      --answer-temperature "$ANSWER_TEMPERATURE"
      --test-temperature "$TEST_TEMPERATURE"
      --max-output-tokens "$RUN_MAX_OUTPUT_TOKENS"
      --save "$RUN_DIR"
      --seed "$SEED"
    )
    if (( ${#OPENAI_MODEL_EXTRA_ARGS[@]} > 0 )); then
      CMD+=("${OPENAI_MODEL_EXTRA_ARGS[@]}")
    fi
    if [[ -n "$SWAP_BANK" ]]; then
      CMD+=(--swap-bank "$SWAP_BANK")
    fi
    if (( $# > 0 )); then
      CMD+=("$@")
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
      printf 'DRY_RUN:'
      printf ' %q' "${CMD[@]}"
      printf '\n'
      continue
    fi

    if ! "${CMD[@]}" 2>&1 | tee "$RUN_LOG"; then
      FAILURES+=("${model} ${problem}")
      echo "FAILED model=$model problem=$problem" >&2
      if rg -q 'insufficient_quota|You exceeded your current quota' "$RUN_LOG"; then
        ABORT_REASON="insufficient_quota"
        echo "ABORTING remaining runs: OpenAI quota exhausted." >&2
        break 2
      fi
    fi
  done
done

if [[ "$DRY_RUN" == "1" ]]; then
  exit 0
fi

JSON_COUNT="$(find "$RUN_DIR" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
if [[ "$JSON_COUNT" != "0" ]]; then
  "${AGGREGATOR[@]}" --in "$RUN_DIR" --out "$CSV_PATH"
  echo
  echo "summary_csv=$CSV_PATH"
else
  echo
  echo "No JSON outputs were produced, skipping aggregation." >&2
fi

if (( ${#FAILURES[@]} > 0 )); then
  FAIL_LOG="${RUN_DIR}/failures.txt"
  printf '%s\n' "${FAILURES[@]}" > "$FAIL_LOG"
  if [[ -n "$ABORT_REASON" ]]; then
    printf '%s\n' "$ABORT_REASON" > "${RUN_DIR}/abort_reason.txt"
    echo "abort_reason=$ABORT_REASON file=${RUN_DIR}/abort_reason.txt" >&2
  fi
  echo "failures=${#FAILURES[@]} log=$FAIL_LOG" >&2
  exit 1
fi

echo "completed=$TOTAL"
