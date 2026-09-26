# Understanding Azure DevOps Builds and Releases

If you're coming from Jenkins or GitLab CI with Artifactory, Azure DevOps Classic
Release Management uses a different mental model. This doc maps the two together and
explains why code like `get_current_release_info` in `devops.py` has to make four API
calls just to answer "what version is deployed to QA?"

## The model you already know (Jenkins/GitLab + Artifactory)

- A CI job builds code and uploads a *versioned artifact* (jar, zip, image) to
  Artifactory under a name + version.
- A separate deploy job pulls that specific version from Artifactory and deploys it.
- The artifact's identity (its version) lives independently in the repository — you can
  always ask Artifactory "what version is this?" and get a direct answer.

## Azure DevOps Classic: two pipeline types glued together

This org's tooling is built on **Azure DevOps Classic Release Management** (the older,
UI-configured Build/Release model — not the newer YAML multi-stage pipelines, which
behave much closer to the Jenkins/Artifactory model). It splits CI and CD into two
separate pipeline concepts:

### 1. Build pipeline

API surface: `_apis/build/definitions`, `_apis/build/builds`.

Compiles/tests code and produces a "build," identified by a `sourceBranch` (the git tag
or branch it built from) and a numeric build ID. If the build's task list includes a
"Publish Build Artifacts" step, files get uploaded to Azure DevOps' *own* internal
artifact storage — not Artifactory. (Note: "Azure Artifacts" is a *different* ADO
feature for npm/NuGet/Maven-style package feeds; it's unrelated to build artifacts
attached to a specific build run.)

Whether this org's builds publish real files, a container image reference, or
effectively nothing tangible (just "this build ran against this git ref") isn't visible
from `devops.py` — that's defined in the build pipeline's task list inside the ADO UI.

### 2. Release pipeline / Release definition

API surface: `_apis/release/definitions`, `_apis/release/releases`.

This is the CD half. A release *definition* declares a sequence of **environments**
(e.g. Dev → QA → Stage → Prod), each with its own approval gates and deploy tasks, and
is wired to an **artifact source** — in this org's case, a Build pipeline
(`a.get("type") == "Build"` in `cmd_create_releases_from_artifact`).

When you create an actual **Release** (one instance of that definition), you pick
*which build's* artifacts to carry through the environments. Deploying an environment
means running that environment's deploy tasks against that specific build's artifacts.
Azure DevOps tracks deployment history per environment — that's what the `deployments`
API returns.

## The critical difference from Artifactory

In Artifactory, the artifact's version **is** its identity — you ask the repo "what's
live" and it just tells you.

In Azure DevOps Classic, a "Release" is not a version. It's a **stateful tracking
record for one build's journey through the environment pipeline**, and it gets an
auto-generated name like `Release-42` that tells you nothing about the software
version.

So to answer "what version is running in QA?", `get_current_release_info`
(`devops.py:640`) has to walk backwards through the entire promotion chain:

```
"what's deployed to QA?"                (deployments API, filtered by environment)
  -> a Release stub, name "Release-42"  (meaningless as a version)
  -> fetch the full Release -> its artifacts[]
  -> each artifact has definitionReference.version.id -> that's a build ID
  -> fetch that build -> read its sourceBranch
  -> strip "refs/tags/" or "refs/heads/" -> THAT's the actual version string
```

Four API round-trips to reconstruct something Artifactory would hand you as one field
on the artifact. That's not this code being clumsy — it's the actual shape of Azure
DevOps Classic Release's data model.

## What's *not* part of this model

Terraform and Flyway (`apply_terraform` / `apply_flyway` in `devops.py`) don't go
through any of this — they're plain GitHub Actions `workflow_dispatch` calls against
the `phoenix` / `phoenix-data-gateway` repos, with no Azure Release or artifact concept
involved at all. Only actual application deployment (`deploy_pipelines`, which drives
real Release environments) goes through the Build -> Release chain described above.

## Finding the ground truth

To see what's *physically* being deployed (a container image? a zip of compiled
binaries? a checked-out ref that gets synced by a script?), this repo's code won't tell
you — that lives in the Azure DevOps UI itself:

1. Open **Pipelines → Releases** in the `Drug Discount Management` project.
2. Open a release definition and find its linked artifact source (a Build pipeline).
3. Open that Build pipeline's task list to see what it actually publishes.
