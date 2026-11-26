#!/usr/bin/env bash
set -euo pipefail

fixture="$(dirname "$0")/missing-identity.json"
expected=$(jq -ce '.response' "$fixture")
url=http://127.0.0.1:8000/api/ai-assistant/v1/conversations

# Keep the body off disk and out of diagnostics, including on unexpected success.
curl -q --noproxy '*' --max-time 10 --silent --show-error \
    --write-out $'\n%{http_code}\n%{content_type}' "$url" |
    jq -Rse --argjson expected "$expected" '
        split("\n") as $lines |
        if ($lines[-2] == ($expected.status | tostring) and $lines[-1] == $expected.content_type)
        then ($lines[:-2] | join("\n") | fromjson? |
              [paths(scalars) as $path | {path: $path, type: (getpath($path) | type)}] |
              sort_by(.path) == ($expected.fields | sort_by(.path)))
        else false end
    ' >/dev/null
printf '%s\n' 'PASS: missing identity rejected with the recorded status, media type and JSON fields.'