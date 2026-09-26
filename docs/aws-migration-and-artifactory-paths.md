# Paths Forward: Possible AWS Migration + Artifactory for SecOps Scanning

Two drivers are in play: a possible future Azure → AWS migration, and a SecOps ask to
adopt Artifactory for scanning tooling. This builds on
[`azure-devops-build-release-practices.md`](./azure-devops-build-release-practices.md)
and [`artifact-types-and-platform-alternatives.md`](./artifact-types-and-platform-alternatives.md) —
read those first for the current-state findings this is based on.

## The key insight: these two drivers converge on the same first move

Right now the artifact is a zip file deployed straight onto Azure App Service/Functions
(confirmed in `artifact-types-and-platform-alternatives.md`) — there's no registry, no
image, nothing Artifactory can meaningfully scan. JFrog Xray scans packages/images it
stores; it can't scan "a zip WebDeploy pushed directly onto a PaaS worker."

**Containerizing is the one change that serves both asks at once**: it gives SecOps
something to actually scan, and it gives you a deployable unit that runs identically
on Azure or AWS. That should be the starting point regardless of what happens with the
AWS decision.

## Path A — Containerize + adopt Artifactory now, stay on Azure (do this first)

- Dockerize each service — start with the 4 satellite repos (`phoenix-data-gateway`,
  `model-n-data-gateway`, `phoenix-privacy-gateway`, `phoenix-snowflake-gateway`;
  single service each, simpler), then work through `phoenix`'s dozen-plus components
  one at a time.
- Replace the `dotnet publish` → "Publish Artifact: deployable" step with
  `docker build` + push to Artifactory. Xray scanning happens at push time, which is
  exactly what SecOps is asking for.
- Deploy target can stay Azure (App Service for Containers, Azure Container Apps, or
  AKS) — no AWS dependency required, so this isn't blocked on the AWS decision at all.
- Low-risk and incremental: each repo migrates independently, and nothing about the
  current App Service/Function App deployment breaks until each one is actually cut
  over.

## Path B — Once containerized, AWS becomes a target decision, not a rewrite

With images sitting in Artifactory, "deploy to AWS" becomes "point a pipeline at
ECS/EKS/Fargate using the same image" rather than re-architecting the app. That's the
real payoff of doing Path A first: it makes the Azure-vs-AWS choice reversible and
cheap instead of a one-way bet made under pressure.

Two things won't be free, though:

- The Terraform investment already in place (`base-tf-plan.yml`/`base-tf-apply.yml`,
  eastus/westus pipelines) transfers as a *pattern* — same PR-gated plan/apply
  workflow — but the HCL itself is Azure-resource-specific and needs to be rewritten
  for AWS resources.
- Azure Functions' triggers (Service Bus, Blob, Timer, Event Grid) don't map 1:1 onto
  AWS Lambda triggers (SQS, S3, EventBridge). That's genuine redesign work for the
  ~12 Function components (see the per-repo breakdown in
  `azure-devops-build-release-practices.md`), not a mechanical swap — worth scoping as
  its own estimate rather than assuming it rides along for free with the container
  move.

## Path C — Retire Azure DevOps Classic Release regardless of cloud

Whether the org stays on Azure or moves to AWS, this is a natural point to also
replace the ADO Build→Release machinery `kald-devops` exists to drive — with something
that fixes the governance gaps already documented (zero approval gates anywhere,
19-environment sprawl on some pipelines) rather than reproducing them on a new
platform. GitHub Actions environments support required-reviewer approval gates
natively; if the team ends up on K8s, Argo CD/Flux gives GitOps-style promotion with
real audit history.

This can happen on its own timeline — it doesn't have to be bundled with the AWS
decision — but it's the same "how do we promote an artifact through environments"
problem being solved twice if tackled separately later.

## Recommended sequencing

Start **Path A now** — it's SecOps-driven, has real deadline pressure, and is fully
cloud-agnostic, so it's not blocked on the AWS question. Treat AWS as a separate
go/no-go to revisit once containerization is underway, since the decision gets
dramatically cheaper once images exist. Hold off on picking ECS vs. EKS vs. Fargate
(or introducing Kubernetes at all, if the team isn't already running it) until that
decision is actually made — don't take on a new orchestration platform speculatively.
