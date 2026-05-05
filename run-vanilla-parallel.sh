#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# run-vanilla-parallel.sh — Page-load benchmark with the system resolver,
# parallelized into N chunks of `data/top-10k-resolvable.csv`.
#
# Vanilla has no DoH/dnscrypt/coredns/sudo path: just spawn N parallel
# `benchmark-har.py vanilla` workers and concatenate the per-entry traces.
#
# Usage:
#   RUNS=3 ./run-vanilla-parallel.sh [--chunks N] [--jobs N]
#
# Flags / env:
#   RUNS=N            Page-load runs per site (default: 3)
#   RUN_OFFSET=N      Add N to every emitted `run` value (default: 0). Use to
#                     extend a prior batch without `(site, run, day)` collisions
#                     — e.g., first batch RUNS=2; top-up RUNS=2 RUN_OFFSET=2.
#   --chunks=N        Split SOURCE_CSV into N equal chunks (default: 40)
#   --jobs=N          Max parallel benchmark processes (default: matches chunks)
#   SOURCE_CSV=path   Input CSV (default: data/top-10k-resolvable.csv)
#   SITE_FILES=...    Override: explicit space-separated CSV list (skips chunking)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNS="${RUNS:-3}"
RUN_OFFSET="${RUN_OFFSET:-0}"
CHUNKS="${CHUNKS:-40}"
SOURCE_CSV="${SOURCE_CSV:-data/crux-top10k-resolvable.csv}"
MAX_JOBS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --jobs=*)   MAX_JOBS="${1#--jobs=}"; shift ;;
        --jobs)     MAX_JOBS="$2"; shift 2 ;;
        --chunks=*) CHUNKS="${1#--chunks=}"; shift ;;
        --chunks)   CHUNKS="$2"; shift 2 ;;
        *) shift ;;
    esac
done

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BATCH_DIR="$SCRIPT_DIR/runs/vanilla-parallel-$TIMESTAMP"
mkdir -p "$BATCH_DIR"
SCRIPT_LOG="$BATCH_DIR/run-vanilla-parallel.log"

exec > >(tee -a "$SCRIPT_LOG") 2>&1
echo "Batch directory: $BATCH_DIR"

# --- Build chunk list ---
if [[ -n "${SITE_FILES:-}" ]]; then
    read -ra CSV_FILES <<< "$SITE_FILES"
    echo "Using provided SITE_FILES (${#CSV_FILES[@]} chunks)"
else
    INPUT_DIR="$BATCH_DIR/inputs"
    mkdir -p "$INPUT_DIR"
    if [[ "$SOURCE_CSV" = /* ]]; then src="$SOURCE_CSV"; else src="$SCRIPT_DIR/$SOURCE_CSV"; fi
    [[ -s "$src" ]] || { echo "ERROR: source CSV not found: $src" >&2; exit 1; }
    total=$(wc -l < "$src")
    per=$(( (total + CHUNKS - 1) / CHUNKS ))
    echo "Splitting $SOURCE_CSV ($total rows) into $CHUNKS chunks of $per rows -> $INPUT_DIR/"
    split -d -a 2 -l "$per" --additional-suffix=.csv "$src" "$INPUT_DIR/chunk-"
    CSV_FILES=("$INPUT_DIR"/chunk-*.csv)
fi

[[ -z "$MAX_JOBS" ]] && MAX_JOBS=${#CSV_FILES[@]}

echo "Parallel jobs:   $MAX_JOBS"
echo "Runs per site:   $RUNS  (run-offset=$RUN_OFFSET → emitted run IDs $((RUN_OFFSET + 1))..$((RUN_OFFSET + RUNS)))"
echo "Chunks:          ${#CSV_FILES[@]}"
echo ""

# --- venv (assumes setup-benchmark.sh has been run at least once) ---
if [[ ! -d "$SCRIPT_DIR/venv" ]]; then
    echo "ERROR: $SCRIPT_DIR/venv not found. Run ./setup-benchmark.sh first." >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/venv/bin/activate"
cd "$SCRIPT_DIR"

# =============================================================================
# Launch benchmarks, throttled to MAX_JOBS in flight.
# =============================================================================
echo "=== Launching ${#CSV_FILES[@]} benchmarks (max $MAX_JOBS parallel) ==="

PIDS=()
LABELS=()

for csv_file in "${CSV_FILES[@]}"; do
    label="$(basename "${csv_file%.csv}")"
    RUN_DIR="$BATCH_DIR/$label"
    mkdir -p "$RUN_DIR"

    RESULTS_CSV="$RUN_DIR/results_har_vanilla.csv"
    ENTRIES_CSV="$RUN_DIR/entries_har_vanilla.csv"
    BENCH_LOG="$RUN_DIR/benchmark-har-vanilla.log"

    echo "  start: $csv_file -> $RUN_DIR"
    python benchmark-har.py vanilla --randomize --runs="$RUNS" \
        --run-offset="$RUN_OFFSET" \
        --sites="$csv_file" \
        --output="$RESULTS_CSV" \
        --entries-output="$ENTRIES_CSV" \
        --log="$BENCH_LOG" \
        > "$RUN_DIR/stdout.log" 2>&1 &

    PIDS+=($!)
    LABELS+=("$label")

    if (( ${#PIDS[@]} >= MAX_JOBS )); then
        wait -n || true
        LIVE_PIDS=(); LIVE_LABELS=()
        for idx in "${!PIDS[@]}"; do
            if kill -0 "${PIDS[$idx]}" 2>/dev/null; then
                LIVE_PIDS+=("${PIDS[$idx]}")
                LIVE_LABELS+=("${LABELS[$idx]}")
            fi
        done
        PIDS=("${LIVE_PIDS[@]}")
        LABELS=("${LIVE_LABELS[@]}")
    fi
done

echo ""
echo "Waiting for remaining workers..."
FAILURES=0
for idx in "${!PIDS[@]}"; do
    if wait "${PIDS[$idx]}"; then
        echo "  done: ${LABELS[$idx]}"
    else
        echo "  FAIL: ${LABELS[$idx]} (exit $?)"
        ((FAILURES++))
    fi
done

echo ""
echo "=== Combining outputs ==="

# Concatenate per-entry traces into one CSV.
COMBINED_ENTRIES="$BATCH_DIR/combined_entries_har_vanilla.csv"
COMBINED_RESULTS="$BATCH_DIR/combined_results_har_vanilla.csv"

# Header from the first available file; data rows from all chunks, sorted by
# (rank, run, started_offset_ms) so trace replay is reproducible.
first_entries=""
for csv_file in "${CSV_FILES[@]}"; do
    label="$(basename "${csv_file%.csv}")"
    f="$BATCH_DIR/$label/entries_har_vanilla.csv"
    if [[ -s "$f" ]]; then first_entries="$f"; break; fi
done

if [[ -n "$first_entries" ]]; then
    head -n 1 "$first_entries" > "$COMBINED_ENTRIES"
    for csv_file in "${CSV_FILES[@]}"; do
        label="$(basename "${csv_file%.csv}")"
        f="$BATCH_DIR/$label/entries_har_vanilla.csv"
        [[ -s "$f" ]] && tail -n +2 "$f"
    done | sort -t',' -k1,1n -k3,3n -k6,6g >> "$COMBINED_ENTRIES"
    echo "  entries: $COMBINED_ENTRIES ($(($(wc -l < "$COMBINED_ENTRIES") - 1)) rows)"
fi

# Same for the aggregated results CSV (sorted by rank, run).
first_results=""
for csv_file in "${CSV_FILES[@]}"; do
    label="$(basename "${csv_file%.csv}")"
    f="$BATCH_DIR/$label/results_har_vanilla.csv"
    if [[ -s "$f" ]]; then first_results="$f"; break; fi
done

if [[ -n "$first_results" ]]; then
    head -n 1 "$first_results" > "$COMBINED_RESULTS"
    for csv_file in "${CSV_FILES[@]}"; do
        label="$(basename "${csv_file%.csv}")"
        f="$BATCH_DIR/$label/results_har_vanilla.csv"
        [[ -s "$f" ]] && tail -n +2 "$f"
    done | sort -t',' -k1,1n -k3,3n >> "$COMBINED_RESULTS"
    echo "  results: $COMBINED_RESULTS ($(($(wc -l < "$COMBINED_RESULTS") - 1)) rows)"
fi

echo ""
echo "=== All benchmarks finished. ==="
echo "  Chunks:   ${#CSV_FILES[@]}"
echo "  Failed:   $FAILURES"
echo "  Batch:    $BATCH_DIR"
