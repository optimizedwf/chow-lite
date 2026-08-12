#!/usr/bin/env bash
# Rebuild the demo terminal segment (requires: playwright, ffmpeg, edge-tts).
# Output: docs/demo-video-v2.mp4 (title + live terminal demo + end card)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Run:  python demo_live.py \"build a small calculator with tests\" > demo_capture/live_flagship.json (capture stdout)"
echo "      python demo_live.py \"handle a customer refund question from the inbox\" > demo_capture/live_lane.json"
echo "Then regenerate timeline.json + terminal.html, record via Playwright, encode with ffmpeg."
echo "See demo_capture/README for the exact steps."
