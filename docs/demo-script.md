# Demo video script — chow-lite (final: ~2:25, ≤4:00 cap, public YouTube, LIVE GCP PROOF required)

> Rules: only first 4 minutes evaluated; English; must show live Google Cloud
> proof in footage (Cloud Run dashboard / .run.app URL in browser / Vertex logs).
> Fresh video — do NOT reuse the DataHub Mr Chow film.

## Timeline (2:25 total — draft v3 already assembled at docs/demo-video-v3.mp4)

| Time | Section | Asset | VO |
|------|---------|-------|----|
| 0:00–0:03 | Title card | seg_title.mp4 (rendered) | — |
| 0:03–1:10 | Terminal demo: live ROUTE (Gemini 3.5 Flash, conf 1.00) → 5-hop EXECUTE → VERIFY artifacts → LEARN events; then Taskmaster lane | seg_terminal.mp4 (66.8s, VO mixed) | "First, route..." (GuyNeural, already mixed) |
| 1:10–1:57 | Architecture pan (ROUTE→EXECUTE→VERIFY→LEARN diagram), Gemma 4 teach hop, one-command API | arch_section.mp4 (47.1s, VO mixed) | "Under the hood..." (3 lines, already mixed) |
| 1:57–2:22 | **LIVE GCP PROOF** — `chow probe https://chow-lite-<id>.run.app`: /health, /v1/submit x2 (real Gemini routing on the deployed API, both SHIP), /v1/jobs, /v1/stats (Firestore-backed) | gcp_segment.mp4 (record after deploy) | "And here is the same system, live on Google Cloud..." |
| 2:22–2:25 | End card | seg_end.mp4 (rendered) | — |

## How to record the GCP segment (needs live deploy — Adam step 3)
1. `gcloud auth login && bash deploy/deploy.sh` → get the `.run.app` URL.
2. `python deploy/demo_probe.py https://chow-lite-XXXX.run.app | tee demo_capture/gcp_transcript.txt`
3. Render gcp_transcript.txt with tools/terminal_template.html (Playwright → webm → mp4) as demo_capture/gcp_segment.mp4.
4. `bash tools/assemble_demo.sh` → docs/demo-video-final.mp4.
5. Upload PUBLIC to YouTube (title: "chow-lite — an evidence-gated agent OS (Google ADK + Gemini 3.5 Flash)"), paste URL into Devpost submission.

## Segment inventory (all rendered, in demo_capture/ + docs/)
- docs/demo-video-v2.mp4 — title + terminal + end (72.8s, standalone)
- docs/demo-video-v3.mp4 — title + terminal + arch + end (119.9s, GCP slot pending)
- tools/terminal_template.html + tools/build_terminal_segment.sh + tools/assemble_demo.sh
