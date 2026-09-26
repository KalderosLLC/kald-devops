# Azure DevOps Build & Release Practices Across Phoenix Repos

An analysis of how `phoenix`, `phoenix-data-gateway`, `model-n-data-gateway`,
`phoenix-privacy-gateway`, and `phoenix-snowflake-gateway` actually build and deploy,
based on direct queries against the Azure DevOps REST API (build definitions, release
definitions, task lists, environment conditions and approvals) and the GitHub API
(Actions workflows, branch protection) — not just what `devops.py` exposes. See
[`azure-devops-release-model.md`](./azure-devops-release-model.md) for the conceptual
background on how ADO Classic Build/Release works.

## The big picture: two CI/CD systems, split by responsibility

Across all 5 repos, GitHub Actions and Azure DevOps don't overlap — they own genuinely
different halves of the pipeline:

- **GitHub Actions** owns: PR-time CI (one workflow per component, often built on
  shared reusable templates like `dotnet-ci-template.yml`), Terraform plan/apply
  (`base-tf-plan.yml` / `base-tf-apply.yml` plus per-region wrappers), Flyway
  migrations (`phoenix-data-gateway` only), NuGet package publishing
  (`phoenix-data-gateway`, `phoenix-snowflake-gateway`), DB refresh scripts, and
  Copilot review/agent bots.
- **Azure DevOps Classic** (Build + Release definitions — the older, UI-configured
  model, not the newer YAML multi-stage pipelines) owns: compiling/packaging the app
  and promoting that package through Dev → ... → Prod. This is the half `devops.py`
  automates (`build_pipelines`, `deploy_pipelines`, `list_pipelines`, etc.).

This is why `devops.py` has to speak two completely different APIs (GitHub REST for
`apply_terraform`/`apply_flyway`, Azure DevOps REST for everything else) — it's
bridging two platforms this org genuinely runs side by side for different purposes,
not redundant tooling.

## Per-repo breakdown

| Repo | ADO build pipelines | ADO release pipelines | Notes |
|---|---|---|---|
| **phoenix** | 27 | 18 | Monolith-of-services: one build+release pair per Azure Function/App (Payment, Notification, EDI, Eligibilities, Pay API/Web, Request API/Web, Admin API/Web, TPA, Enforcement, Geo, Kyriba, Cache, ...). Plus 2 YAML-based orchestration builds (`KalderosLLC.phoenix` x2, `MT-Deploy`) for multi-tenant deploy orchestration. |
| **phoenix-data-gateway** | 1 | 1 | Clean, modern template. Also has its own Flyway CI/migration workflows and publishes a NuGet package. |
| **model-n-data-gateway** | 1 | 1 | Same clean template as phoenix-data-gateway — but QA/Test/Stage/Prod/Demo show no deployment history at all (blank `current_version`/`deployed_at`). Reads as a newer integration still confined to Dev/Automated/Load/Preview, never promoted past that. |
| **phoenix-privacy-gateway** | 2 | 2 | Unusually has *two parallel* build+release pairs: `priv-api` / "Release Privacy Api" and `kalderos-privacy-gateway` / "Release Privacy Gateway". Both builds carry identical leftover cruft (see below) — worth confirming with the team whether one pair is dead and should be retired. |
| **phoenix-snowflake-gateway** | 1 | 1 | Clean modern template, also publishes a NuGet package. |

Build pipeline counts came from `list_build_pipelines`; release pipeline counts from
`list_pipelines` (both scoped via `REPOSITORIES`/`PIPELINES` to these 5 repos).

## How deployment actually works

Every release definition inspected via `GET .../release/definitions/{id}` has the
**same automatic-promotion shape**:

1. A build completing triggers an `artifactSource`-type definition trigger, which
   automatically creates a new **Release**.
2. That Release auto-deploys to **Dev** — its environment has a condition of
   `conditionType: event`, name `ReleaseStarted`.
3. Dev succeeding auto-deploys to **"Automated"** — its condition is
   `conditionType: environmentState`, chained off Dev's result.
4. **Every other environment — QA, Test, Stage, Preview, Demo, Load, Perftest, Prod,
   Prod-West — has zero conditions.** They only deploy when something explicitly
   triggers them: this CLI's `deploy_pipelines`, or a human in the ADO UI.

Deploy mechanism is classic Azure PaaS, not containers:

- **"Deploy Azure App Service"** for the gateway/API-style services
  (`phoenix-data-gateway`, `model-n-data-gateway`, both privacy-gateway pipelines).
- **"Azure Function App Deploy: `<specific-function-app-name>`"** for phoenix's many
  Function components — each environment targets its own dedicated, separately-named
  Azure Function App resource (e.g. `func-notification-6253f` for Dev,
  `func-notification-77b9d` for Automated, ...), not a shared deployment slot.

Classic build definitions (the `admin-api`, `phoenix-datagw`, `model-n-datagw`,
`kalderos-privacy-gateway`, `priv-api`, and `phoenix-snowflakegw` build pipelines) all
run the same core .NET task sequence: `dotnet restore` → `dotnet build` →
`dotnet publish` → **Publish Artifact: `deployable`**. That last task is Azure
DevOps' built-in artifact-publish task — it uploads a real package (not just a
reference) into Azure DevOps' own internal artifact storage, which the linked Release
definition then picks up by build ID.

## The governance gap

**Every environment on every release definition inspected has `approvers: 0`.** There
is no pre-deployment approval gate configured anywhere in Azure DevOps — including
Prod, on every repo. Anything that can call the deploy API (this CLI, or anyone with
pipeline permissions) can push straight to production with no platform-enforced
sign-off.

This pairs with a matching gap on the GitHub side. All 5 repos have identical branch
protection on `main`:

```json
{"enforce_admins": true, "required_reviews": 1, "required_status_checks": []}
```

One required approving review, admins can't bypass it — but `required_status_checks`
is empty on every single repo, despite each having a dozen-plus CI workflows defined
(per-component `*-ci.yml` workflows, Terraform plan/apply, etc.). None of that CI is
actually wired as a *required* check, so a PR can merge to `main` with red CI.

## Technical debt / inconsistency signals

- **"-Terraform" duplicate environments**: several phoenix release pipelines (Admin
  Api/Web, Pay Api/Web, Request Api/Web) have both a plain environment and a
  `-Terraform`-suffixed twin (e.g. `Prod` and `Prod - Terraform`) running the same
  task list. Reads as a half-finished migration where a new stage was added alongside
  the old one instead of replacing it in place.
- **Dead-but-present environments**: `Dev - ARM DEPRECATED` is literally named
  deprecated and is still a live environment on both Release Admin Api and Release
  Admin Web.
- **Naming drift** across the 18 phoenix release pipelines: `Load` vs `load`,
  `Prod - West` vs `Prod West` (the Edi-function pipeline specifically drops the
  hyphen that every sibling pipeline uses).
- **Tenant-specific stages baked directly into environment names** rather than
  parameterized: `Stage, Lilly - Terraform`, `Stage, Gilead - Terraform`,
  `Prod, Gilead - Terraform` on Release Pay Web — which alone has **19 environments**,
  the largest of any pipeline surveyed.
- **Leftover disabled build tasks**: the `admin-api`, `priv-api`, and
  `kalderos-privacy-gateway` classic builds all still contain a *disabled*
  SonarQube / code-coverage / old-style `Restore`-`Test`-`Test Continued` task chain
  sitting next to the enabled `dotnet restore`/`build`/`publish` steps that clearly
  superseded them — switched off, never deleted.
- **Newer services got a cleaner template**: `phoenix-data-gateway`,
  `model-n-data-gateway`, and `phoenix-snowflake-gateway`'s builds have none of that
  cruft — a standard ~9-10 environment shape (Dev/Automated/QA/Preview/Demo/Load/
  Perftest/Prod/Stage/Test) with no disabled steps. The team clearly improved its
  template over time but never went back and retrofitted the original phoenix
  pipelines.

## Lower-confidence observation

Several `phoenix` build pipelines (`dwolla-function`, `KalderosLLC.phoenix.dbrefresh`,
`pay-web-clone`, `request-web-clone`) don't have an obviously corresponding release
pipeline under the ADO folder this tooling scopes to (`\phoenix`, per the
`folder_path` constant in `devops.py`). This isn't necessarily a gap — they could have
a release definition filed under a different ADO folder that this analysis doesn't
see, or genuinely not need one (e.g. `dbrefresh` may just be a one-shot script rather
than something continuously deployed). Flagging it rather than asserting it as a
finding.

## Method

All of the above came from live queries during this analysis:

- `GET https://dev.azure.com/{org}/{project}/_apis/build/definitions/{id}` — process
  type (Classic vs YAML), repository/trigger config, and (for Classic) the full
  `process.phases[].steps[]` task list with each step's `enabled` flag.
- `GET https://vsrm.dev.azure.com/{org}/{project}/_apis/release/definitions/{id}` —
  each environment's `preDeploymentApprovals`, `conditions`, and `deployPhases[].
  workflowTasks[]`.
- `gh api repos/KalderosLLC/{repo}/actions/workflows` and
  `gh api repos/KalderosLLC/{repo}/branches/main/protection` for the GitHub side.
- `kald-devops list_build_pipelines` / `list_pipelines` (this repo's own CLI) for the
  repo-to-pipeline mapping and current per-environment deployment state.
