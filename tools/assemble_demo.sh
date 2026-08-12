#!/usr/bin/env bash
# Assemble the final chow-lite demo video from segments.
# Place the recorded GCP segment at demo_capture/gcp_segment.mp4 then run:
#   bash tools/assemble_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=docs/demo-video-final.mp4
GCP=demo_capture/gcp_segment.mp4

if [[ ! -f "$GCP" ]]; then
  echo "MISSING demo_capture/gcp_segment.mp4 — record the live GCP proof first:"
  echo "  python deploy/demo_probe.py https://YOUR-APP.run.app | tee demo_capture/gcp_transcript.txt"
  echo "  (render via tools/terminal_template.html -> Playwright -> ffmpeg, see README below)"
  exit 1
fi

# Re-encode every segment to a common codec/rate for clean concat.
for s in seg_title seg_terminal arch_section gcp_segment seg_end; do
  src="demo_capture/$s.mp4"; dst="demo_capture/${s}_norm.mp4"
  ffmpeg -y -v error -i "$src" -c:v libx264 -pix_fmt yuv420p -crf 20 -preset veryfast \
         -c:a aac -b:a 128k "$dst"
done

printf "file 'demo_capture/seg_title_norm.mp4'\nfile 'demo_capture/seg_terminal_norm.mp4'\n" > demo_capture/final_list.txt
printf "file 'demo_capture/arch_section_norm.mp4'\nfile 'demo_capture/gcp_segment_norm.mp4'\n" >> demo_capture/final_list.txt
printf "file 'demo_capture/seg_end_norm.mp4'\n" >> demo_capture/final_list.txt

ffmpeg -y -v error -f concat -safe 0 -i demo_capture/final_list.txt \
       -c:v libx264 -pix_fmt yuv420p -crf 20 -preset medium \
       -c:a aac -b:a 128k -movflags +faststart "$OUT"
echo "FINAL VIDEO: $OUT ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")s)"
