# Infra/Terraform Impact: GH → Artifactory → ACA for All APIs and Functions

A concrete follow-up to
[`path-a-containerization-deep-dive.md`](./path-a-containerization-deep-dive.md),
scoped specifically to: **all** APIs and Functions move to Azure Container Apps (ACA),
pulling images from Artifactory (not Azure Container Registry). This walks through
what actually changes in the Terraform, resource by resource.

## New resource types

- **`azurerm_container_app_environment`** — the shared hosting boundary (roughly
  analogous to an App Service Plan). The natural granularity is **one per deployment
  stage** (Dev, QA, Stage, Prod, etc.), hosting *all* the services for that stage —
  not one per app. This is a design decision worth making deliberately rather than
  defaulting into it.
- **`azurerm_container_app`** — one per service per environment, replacing today's
  `azurerm_linux_web_app`/`azurerm_function_app` resources.
- **KEDA scale rules** (`template.scale_rule` blocks inside each Container App) for
  the ~12 containerized Functions — genuinely new HCL. Consumption-plan Functions
  autoscaled invisibly; ACA requires explicitly declaring a scale rule matching each
  Function's actual trigger (HTTP, queue, Service Bus, timer). This is real
  per-Function analysis work, not boilerplate.
- **Ingress config** per app (`ingress` block: external/internal, target port,
  revision traffic split) plus new custom-domain/TLS bindings — ACA's domain-binding
  model differs from App Service's `azurerm_app_service_custom_hostname_binding`, so
  any custom domains need re-creating, not just moved.

## The environment-sprawl decision this forces

1:1-replicating today's environment counts (9-10 for the clean newer services, up to
**19** for Release Pay Web per the earlier build/release audit) means 200+
`azurerm_container_app` resource instances. This migration is a natural forcing
function to fix the sprawl documented in
[`azure-devops-build-release-practices.md`](./azure-devops-build-release-practices.md) —
consolidate onto the clean ~9-10 environment shape the newer gateway services already
use, rather than carrying phoenix's `-Terraform` duplicates and
`Dev - ARM DEPRECATED` dead environments into the new model. Worth deciding explicitly
rather than discovering it mid-migration.

## The Artifactory-not-ACR registry auth gap

This is the sharpest new problem the "skip ACR" choice introduces. ACR-backed
Container Apps get zero-secret pulls via Managed Identity (an `AcrPull` role
assignment) — that doesn't exist for a third-party registry. Pulling from Artifactory
means each Container App needs a `registry` block with `server` + credentials, and
**a real secret (an Artifactory access token) has to be created, stored in Key Vault,
and rotated** — a new secret-management surface that didn't exist with either
WebDeploy (publish profiles) or an ACR-based setup.

Alternative worth considering: keep a thin `azurerm_container_registry` as a
pull-through cache in front of Artifactory. Artifactory still does the ingest-time
Xray scan (the actual SecOps ask), but ACA pulls via Managed Identity with no secret.
That hybrid is worth weighing against "fewer moving parts, one registry" before
committing.

## What gets reused vs. re-pointed vs. retired

- **Reused as-is**: `azurerm_application_insights`, `azurerm_key_vault`, the
  databases, and — usefully — any existing `azurerm_user_assigned_identity`
  resources. User-assigned identities are decoupled from the compute resource, so the
  same identity can just be attached to the new Container App's `identity` block; no
  need to recreate the identity, just re-point the attachment.
- **Needs re-pointing**: every `azurerm_role_assignment`/Key Vault access policy
  currently scoped to an App Service's or Function App's `principal_id` needs to
  target the new Container App's principal instead (unless already on a reused
  user-assigned identity, in which case this is a non-event).
- **Needs rewiring, not just moving**: the Functions' `AzureWebJobsStorage` storage
  account requirement doesn't disappear — it still needs to be wired into each
  containerized Function via an ACA secret/env var, just no longer through the
  Function App's special-cased settings slot. Same story for Application Insights:
  App Service's automatic site-extension instrumentation has no ACA equivalent, so
  the App Insights SDK (or OTLP export) needs to be explicitly wired into each
  container image.
- **Retired**: `azurerm_service_plan`/`azurerm_linux_web_app`/
  `azurerm_linux_function_app` (or Windows equivalents) get deleted from state once
  each service is cut over.

## Migration mechanics

Terraform can't convert an App Service resource into a Container App in place — it's
delete-and-create, not a resource type change. This has to be a **blue/green rollout
per service** (stand up the new Container App resource alongside the old App Service,
cut traffic over, then remove the old resource from state), not a single
`terraform apply` across everything. Consistent with the incremental, one-service-at-
a-time approach already recommended for the containerization work itself.

## Reintroduces the earlier PAT/service-principal decision

GitHub Actions now needs a way to authenticate to Azure to actually deploy (update the
Container App's image reference, whether via `az containerapp update` or a Terraform
apply). This is exactly the scenario the earlier PAT/service-principal conversation
was about — the right answer here is a **GitHub OIDC federated credential to an Entra
service principal**, scoped to just the Container Apps it needs to touch, rather than
another static secret sitting in GitHub. Worth keeping this decision consistent with
whatever was decided for `AZURE_DEVOPS_EXT_PAT`.
