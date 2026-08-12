
# Archon → Nine Workflow Mapping

| Archon Workflow | Nine WF / Chain | Nine Status | Key Pattern to Borrow |
|-----------------|-----------------|-------------|----------------------|
| archon-piv-loop | research-plan-build-review-teach (chain) | ✅ exists | explore→plan→refine→implement→review→fix→finalize |
| archon-fix-github-issue | debug + debug-investigate | ← build | 22-node: extract→fetch→classify→research→investigate→plan→implement→validate→review(parallel)→self-fix |
| archon-comprehensive-pr-review | review-multi | ← build | 5 parallel agents (code/error/test/comments/docs) → synthesize → implement-fixes |
| archon-ralph-dag | plan-deep + build | ← build | PRD→validate→implement→verify→report |
| archon-adversarial-dev | adversarial (NEW) | ← ADD | GAN: planner → generator vs evaluator loop, hard pass/fail thresholds |
| archon-architect | analyze | ← build | scan-metrics→analyze→plan→simplify→validate→fix→create-pr |
| archon-assist | respond | ✅ exists | single-node universal answer |
| archon-create-issue | investigate (NEW) | ← ADD | classify→fetch-template→git-context→dedup→investigate→reproduce→draft→create |
| archon-feature-development | build + deploy-check | ← build | implement→create-pr→verify-pr-base |
| archon-idea-to-pr | ideate→plan→build→review (chain) | ← build | |
| archon-interactive-prd | plan-deep | ← build | foundation-gate→research→deepdive-gate→technical→scope-gate→generate→validate |
| archon-issue-review-full | debug + review-multi | ← build | investigate→implement→verify→review(5 parallel)→synthesize→fixes |
| archon-plan-to-pr | plan→build→review (chain) | ← build | confirm-plan→implement→validate→finalize-pr→review→fixes→summary |
| archon-refactor-safely | refactor | ← build | scan→analyze→plan→execute→validate→fix→verify-behavior→create-pr |
| archon-remotion-generate | video (PARKED) | - | check-project→generate→render-preview→render-video→summary |
| archon-resolve-conflicts | resolve-conflicts (NEW) | ← ADD | single-node conflict resolver |
| archon-smart-pr-review | review-adaptive (NEW) | ← ADD | classify complexity → route to only-relevant agents (adaptive) |
| archon-test-loop-dag | test | ← build | setup→loop(iterative until complete)→report |
| archon-validate-pr | validate (NEW) | ← ADD | fetch-pr→resolve-paths→code-review→classify-testability→e2e-test→cleanup→report |
| archon-workflow-builder | compose | ← build META-WF | scan→extract-intent(JSON)→generate-yaml→validate→save |

## Community Workflows on archon.diy (not in default repo)

| Community WF | Nine equivalent | Pattern |
|---------------|-----------------|---------|
| archon-idea-to-wo (lamachine) | ideate | 8-node idea→work-order with 4 approval gates |
| archon-smart-mr-review (lraphael) | review-adaptive (GitLab) | GitLab counterpart of smart-pr-review |
| archon-resolve-mr-conflicts (lraphael) | resolve-conflicts (GitLab) | GitLab counterpart |
| archon-comprehensive-mr-review (lraphael) | review-multi (GitLab) | GitLab counterpart |
| harness-score (seanrobertwright) | audit | 10-check deterministic readiness audit |
| pocock-skills-workflow-family (seanrobertwright) | spec/plan/review/test/refactor/debug (family) | Real skill.md files mounted into nodes |
| token-max-site-factory (thesmokedev) | content-factory (NEW) | scan→expand→validate SEO pages |
| image-node-factory (thesmokedev) | image-factory (NEW) | brief→grounded prompt pack→render |

## NEW workflows to add to roadmap (from Archon gap):

1. adversarial — GAN-inspired dev (planner→generator→evaluator, hard thresholds, sprint loop)
2. investigate — issue reproduction (classify→context→dedup→investigate→reproduce→draft)
3. resolve-conflicts — merge conflict auto-resolution (rebase→resolve simple→complex→report)
4. review-adaptive — smart review that classifies complexity first, runs only relevant agents
5. validate — e2e PR validation (fetch→review→classify-testability→e2e-test→cleanup→report)
6. content-factory — programmatic SEO/geo page generation (LATER)
7. image-factory — grounded image prompt pack generation (LATER)
