# ADAM RUNBOOK — the 3 things only you can do

Everything else is built, tested (99/99), pushed, and demo-ready.
These three steps need **your** accounts. ~30 minutes total.

---

## 1. Devpost registration (5 min) — do FIRST
Deadline Aug 31 2026 17:00 PDT; field already 1,760+ participants.

1. Go to https://allthingsagentichackathon.devpost.com
2. Sign in as **optimizedworkflowdev** (your existing Devpost account — verified live)
3. Click **Join hackathon** (register for challenge id 30845)
4. Pick track **TASKMASTER** at registration if asked

## 2. GCP credits form (10 min) — do SECOND
Deadline **Aug 28 2026 12:00pm PT** (3 days before submission deadline).
One code per entrant; first-come; auto-declined without track+pitch.

- Form: https://forms.gle/5PtXmw1dSbDnpYke9  (Resources tab; = old riGhgDSHkHeMx8Ca6 link)
- Track: **TASKMASTER**
- Suggested pitch (1–2 sentences, paste):
  > "nine: an open-source, evidence-gated agent operating system
  > (ROUTE→EXECUTE→VERIFY→LEARN) on Google ADK 2 + Gemini 3.6 Flash.
  > Autonomous multi-hop workflows with SHIP/FIX/BLOCK evidence gates,
  > deployed on Cloud Run + Firestore."
- $150 credits, redeem BEFORE Sep 3, 90 days to use.

## 3. GCP deploy + auth (15 min) — after credits land
gcloud 580.0.0 is **already installed** on this Mac. You just auth:

```bash
gcloud auth login            # browser sign-in, use the billing account
gcloud auth application-default login
gcloud config set project <your-project-id>   # or create one:
gcloud projects create nine-$(date +%s) --name="nine"
gcloud services enable run.googleapis.com firestore.googleapis.com
cd ~/nine-work/nine
bash deploy/deploy.sh        # one-command Cloud Run deploy (scale-to-zero)
```

Then tell me the `.run.app` URL — I will:
- record the live-GCP-proof segment of the demo video
- post the live URL in the Devpost submission
- verify Firestore rules + billing alerts

---

## Timeline
- **Aug 26**: credits should be applied (72 business-hr review)
- **Aug 28 12:00 PT**: credit form deadline
- **Aug 31 17:00 PDT**: submission deadline (video ≤4min, PUBLIC YouTube,
  live GCP proof REQUIRED in footage, repo frozen after submit)

## Stage-3 bonus checklist (worth +0.2 each, max +1.0)
- [ ] Public X/LinkedIn post with #AllThingsAgenticHackathon
- [ ] Public blog/podcast/video about the build ("created for purposes of entering this hackathon")
- [ ] Second Google AI model (Veo/Gemma/Lyria...) — could add Gemma to the teach hop
