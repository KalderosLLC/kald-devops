# What's Actually Being Built, and Could This Move to Artifactory + Kubernetes?

A follow-up to
[`azure-devops-build-release-practices.md`](./azure-devops-build-release-practices.md):
what artifact type is actually produced and deployed across
`phoenix`/`phoenix-data-gateway`/`model-n-data-gateway`/`phoenix-privacy-gateway`/
`phoenix-snowflake-gateway`, and whether Azure DevOps could reasonably be replaced with
Artifactory + a Kubernetes cluster. Confirmed by pulling the actual task **inputs**
(not just task names) from the release deploy steps via the Azure DevOps REST API,
rather than inferring from task display names alone.

## What's actually being built and deployed

**Not Docker images.** The build produces a **zip file of compiled .NET binaries**
(`dotnet publish` → zipped), and the release deploy tasks push that zip directly onto
Azure App Service / Azure Functions via WebDeploy ("zip deploy") — Microsoft's classic
PaaS deployment mechanism:

- Phoenix Data Gateway's deploy task input: `Package = .../phoenix-data-gateway-api.zip`.
  The task *does* expose `DockerNamespace`/`DockerRepository`/`DockerImageTag` fields
  (it's a multi-purpose task supporting either mode) — but they're empty. The zip path
  is what's actually populated.
- Notification-Function's deploy task input: `appType = functionAppLinux`,
  `package = .../kalderos-notification-functions` — again a package path, not a
  container image reference. If this were container-based, `appType` would read
  `functionAppLinuxContainer` and there'd be an image reference instead of `package`.

So: real compiled code, zip-deployed straight onto managed Azure compute. No registry,
no image, no Kubernetes anywhere in this chain today.

## Are these "basic microservices (functions)"?

Mixed, split cleanly by deploy task type:

- **Genuine Azure Functions (FaaS)**: Notification, EDI, Payment, Eligibilities,
  Enforcement, Geo, Kyriba, TPA, Client-Transfer-File, Claims-Event, Cache — about a
  dozen event/HTTP-triggered functions, all living inside the `phoenix` monorepo.
- **Regular always-on web APIs/apps (Azure App Service, not FaaS)**: admin-api/web,
  pay-api/web, request-api/web (also in `phoenix`), plus all 4 satellite repos
  (phoenix-data-gateway, model-n-data-gateway, phoenix-privacy-gateway x2,
  phoenix-snowflake-gateway).

So `phoenix` is really a monorepo hosting both styles side by side, each with its own
independent Build+Release pair; the 4 satellite repos are each a single
App-Service-hosted API.

## Could Azure DevOps be replaced with Artifactory + a K8s cluster?

Technically, yes — that's a completely standard pattern (build → push a container
image to Artifactory → GitOps-deploy to K8s via Argo CD/Flux/Helm), and it would fix
some real problems already documented in the companion doc: containers are
reproducible in a way "whatever patch level Azure's App Service workers happen to be
on" isn't, and image tags in a real registry replace the convoluted
Release → Build → sourceBranch chain (see `azure-devops-release-model.md`) with
something Artifactory already does natively.

The tradeoff that matters most: the ~12 Azure Functions get real, currently-free
platform value (consumption autoscaling, scale-to-zero, declarative triggers for
Service Bus/Blob/Timer/HTTP) that would have to be explicitly rebuilt on K8s (KEDA for
event-driven autoscaling, or Knative) — and a K8s cluster is infrastructure *you* now
own, patch, and secure, which this team doesn't need at all today since App
Service/Functions abstract that away entirely. The App-Service-hosted APIs would be a
much more mechanical lift (they're already standard ASP.NET Core apps —
containerizing them is straightforward), but it's also worth weighing that the
sharpest problems the build/release analysis actually surfaced — zero approval gates
on Prod, CI that exists but isn't required, dead `-Terraform` duplicate environments —
aren't platform problems. Azure DevOps approval gates and GitHub required status
checks can be turned on today, cheaply, without a multi-quarter re-platform; a K8s
migration wouldn't fix any of that unless the guardrails were deliberately designed
back in anyway.

## Method

Findings confirmed by pulling task `inputs` (not just task names) via:

```
GET https://vsrm.dev.azure.com/{org}/{project}/_apis/release/definitions/{id}?api-version=7.1
```

then inspecting each environment's `deployPhases[].workflowTasks[].inputs` for
`Package`/`appType`/`DockerImageTag`-style fields, for both an App-Service-deployed
pipeline (Phoenix Data Gateway, id 121) and a Function-App-deployed pipeline
(Notification-Function, id 70).
