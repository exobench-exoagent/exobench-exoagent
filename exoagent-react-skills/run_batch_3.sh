#!/usr/bin/env bash
set -u

RUN_KIND="ReAct+Skills"

FITS_DIRS=(
  "/workspace/exoagent/observation/KELT-20 b"
  "/workspace/exoagent/observation/Qatar-1 b"
  "/workspace/exoagent/observation/Qatar-10 b"
  "/workspace/exoagent/observation/TRES-1 b"
  "/workspace/exoagent/observation/TrES-2 b"
)

PLANETS=(
  "KELT-20 b"
  "Qatar-1 b"
  "Qatar-10 b"
  "TRES-1 b"
  "TRES-2 b"
)

if [ "${#FITS_DIRS[@]}" -ne "${#PLANETS[@]}" ]; then
  echo "FITS_DIRS and PLANETS must have the same number of entries." >&2
  exit 1
fi

total="${#FITS_DIRS[@]}"
failures=0

for ((i = 0; i < total; i++)); do
  input_dir="${FITS_DIRS[$i]}"
  planet="${PLANETS[$i]}"
  run_id="$planet ($RUN_KIND)"

  echo "[$((i + 1))/$total] Running main.py"
  echo "$input_dir"
  echo "$planet"
  echo "Run ID: $run_id"

  if printf '%s\n%s\n' "$input_dir" "$planet" | EXOAGENT_RUN_ID="$run_id" python main.py; then
    echo "[$((i + 1))/$total] Finished successfully"
  else
    status=$?
    failures=$((failures + 1))
    echo "[$((i + 1))/$total] Failed with exit code $status" >&2
  fi

  echo
done

if [ "$failures" -gt 0 ]; then
  echo "$failures of $total runs failed." >&2
  exit 1
fi

echo "All $total runs finished successfully."
