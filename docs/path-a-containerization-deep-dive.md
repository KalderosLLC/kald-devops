# Path A Deep Dive: Containerization Level of Effort, Deploy Target, and Infra Impact

A closer look at **Path A** from
[`aws-migration-and-artifactory-paths.md`](./aws-migration-and-artifactory-paths.md):
getting containers producing images and wired into Artifactory scanning, independent
of the AWS decision. Answers the concrete follow-up questions: level of effort, how to
deploy, whether Azure Functions disappear, AKS vs. something simpler, infrastructure
impact, and what gets introduced vs. abandoned.

## Level of effort to containerize

The two workloads containerize very differently in difficulty.

**The ~10 App-Service-hosted APIs** (admin-api, pay-api, request-api, the 4 gateway
services) are the easy case *if* they're already .NET Core/5+/8 rather than legacy
.NET Framework. Modern ASP.NET Core containerizes mechanically — multi-stage Dockerfile
(SDK image builds/publishes, runtime image runs it), and the config model barely
changes since App Service "Application Settings" are already just environment
variables under the hood, which containers consume the same way.

Once a template Dockerfile exists: roughly **half a day to 2 days per service** to get
working parity, with the *first* one taking longer (a few days) to nail the base image
and multi-stage pattern.

One real unknown that can't be confirmed without checking each pipeline: the build
tasks show both "Use .NET" and "Use .NET Core sdk" steps across different pipelines —
if any of these are actually .NET Framework 4.x (Windows-only), that's a much bigger
lift (Windows containers, or a framework upgrade first) and would significantly change
this estimate. Worth checking per-service before committing to a timeline.

**The ~12 Azure Functions** are mechanically similar effort per-service (Microsoft
ships official `mcr.microsoft.com/azure-functions/*` base images specifically for this
scenario), but they carry a design question, not just a packaging question — see
below.

**Total**: several weeks of calendar time for a small team, done incrementally one
service at a time — not a big-bang cutover, consistent with the incremental framing in
the parent doc. The bigger cost is usually validation per service (DB connectivity,
managed identity auth, App Insights wiring), not writing the Dockerfile itself.

## Would Functions disappear?

Not necessarily — and containerizing them away from the Functions model is not
recommended as part of Path A. Two real options exist:

1. **Containerize them as Functions.** Azure Container Apps has native support for
   running the Functions *runtime* in a container with KEDA-based scale rules (HTTP,
   queue, timer), so the trigger/binding programming model is kept and the only change
   is gaining a portable artifact. This is Microsoft's own documented answer to
   "containerize my Functions."
2. **Rewrite them into always-on workers** (a queue-triggered Function becomes a
   process that polls the queue itself). This is a real rewrite, trades scale-to-zero
   for always-on compute cost, and only pays off if the goal is to get *off* the
   Functions programming model entirely.

Recommendation: do (1) for Path A, defer (2). If an AWS migration actually happens
later, Functions need a real redesign regardless (Lambda's trigger model doesn't map
1:1 to Azure's), so rewriting them now to "future-proof" for AWS pays a cost today for
a benefit that may never be needed.

## AKS or something simpler?

Something simpler — **Azure Container Apps (ACA)**, not AKS.

ACA is Microsoft's managed "serverless containers" layer (KEDA under the hood): scale-
to-zero and event-driven autoscaling without owning a cluster (no node pools, no
cluster upgrades, no K8s RBAC to manage). This team runs zero Kubernetes today, and
AKS's operational tax (patching, networking, ingress, cluster-level security) isn't
justified by anything in the prior analysis — it would repeat the same
"don't introduce infrastructure speculatively" mistake already flagged for K8s in
`artifact-types-and-platform-alternatives.md`. ACA's mental model also maps reasonably
well onto **AWS Fargate/ECS** if the AWS move materializes, so choosing it doesn't
create a dead end for that future decision.

There's an even more conservative option worth naming: **App Service for Containers**.
The deploy task Azure DevOps is already using (`AzureRmWebAppDeployment`) supports
pointing at a container image instead of a zip *right now* — same task, same App
Service resource, just flip one input from `Package` to a container reference (this
was confirmed directly by inspecting the task's available input fields in
`artifact-types-and-platform-alternatives.md`). That is the true minimal version of
Path A: containerize, push to Artifactory, keep every existing App Service/ADO Release
pipeline exactly as-is, and just change what gets deployed. No new Azure resource
type, no ACA, no Terraform rewrite.

Suggested approach: start there for the first 1-2 services to prove out the container
build pipeline and Artifactory scanning gate, then decide whether ACA's extra
scale-to-zero/event-driven benefits are worth the infra migration for the rest.

## Infrastructure impact (Terraform)

This scales with which option above is chosen:

- **App Service for Containers route**: minimal — no new Terraform resource types, no
  new Azure service. Just a registry (Artifactory and/or ACR) and a CI change.
- **ACA route**: real infra work — new `azurerm_container_app_environment` plus
  per-service `azurerm_container_app` resources replacing the current
  `azurerm_app_service`/`azurerm_function_app` resources, meaning **every
  environment's Terraform module gets rewritten**, not just re-parameterized. Given
  the earlier finding of 8-19 environments per pipeline (see
  `azure-devops-build-release-practices.md`), it's worth confirming this isn't fully
  duplicated per environment in the Terraform today — it's likely templated/
  parameterized already, but that's an assumption to verify, not a given. Managed
  Identity and RBAC bindings should carry over conceptually (ACA supports Managed
  Identity the same way App Service does) but every binding needs re-pointing.
  Application Insights auto-instrumentation, if currently wired via the App Service
  platform integration, needs to move to explicit SDK/OpenTelemetry wiring in the
  container.

## What gets introduced vs. abandoned

**Introducing** (regardless of route): Dockerfiles plus a container build/push step
per service, a registry (Artifactory, possibly alongside ACR), Xray scanning gates in
CI — this last one is the actual SecOps deliverable.

**Introducing** (ACA route only): Container Apps Environment(s), KEDA-based scale
rules for the containerized Functions, new Terraform resource types for the compute
layer.

**Abandoning**: the WebDeploy zip-deploy mechanism (`Package`/`UseWebDeploy` task
inputs) goes away entirely either way. On the ACA route, the App Service Plan/Function
App resources themselves get retired.

**Not required to abandon**: Azure DevOps Classic Release pipelines. Retiring them
isn't necessary for Path A — the minimal (App Service for Containers) route literally
keeps them, just changes the deploy task's input. Retiring ADO Release in favor of
GitOps-style promotion is still Path C from `aws-migration-and-artifactory-paths.md`,
and should stay decoupled from this decision rather than being bundled with it.
