# Deployment guidance

## Scope and sources of truth

This directory contains the older Helm/Kubernetes deployment tree.
Its duplicate GitHub Actions workflows have been removed. Treat it as a
consumer-specific, partly-retired surface, not as proof of what is deployed. Check the named
workflow or manifest consumer before editing a similarly named chart, values
file, manifest, or script.

The current status evidence is the deployment-source entry in
[`docs/engineering/gotchas.md`](../docs/engineering/gotchas.md). Only `dev` is
live, on AWS EKS (`nous-dev-cluster`, `us-east-1`). The live Argo CD source is
[`infrastructure/argocd/applications/aws-dev.yaml`](../infrastructure/argocd/applications/aws-dev.yaml):
application `nous-dev-aws`, tracking `develop`, rendering
`infrastructure/helm/knowledge-graph-analytics` with `values.yaml` plus
`values-aws.yaml` into namespace `multimodal-rag-system`. The EKS root app
([`aws-dev-root.yaml`](../infrastructure/argocd/applications/aws-dev-root.yaml))
excludes `root.yaml` and `dev.yaml`, so `dev.yaml` (`nous-dev`,
`values-dev.yaml`, namespace `rag-dev` on the retired DOKS cluster) is frozen
rollback material, not a live source. The `staging` and `production` Argo CD
apps were retired 2026-04-29 (PR #442); their values files remain as render
fixtures. Qdrant is removed; any Qdrant reference left in this tree is dead.

The current CI ownership and retirement notes are in
[`.github/workflows/README.md`](../.github/workflows/README.md).
[`deployment/README.md`](README.md) is current: it marks this directory as
legacy and points to the live path.

## Invalid patterns

- Do not call the staging or production values files, the legacy
  `deployment/` chart, or the old deployment workflow live without proving a
  current consumer. Do not revive retired Argo CD paths or infer reachability
  from a filename, a directory tree, or an old README.
- Do not copy or synchronize same-named deployment and infrastructure
  manifests blindly. Trace the workflow, Argo CD application, or other
  concrete consumer first; keep Qdrant references explicitly historical until
  a current consumer proves otherwise.
- Do not turn a lint, render, kubeval, or Helm template result into permission
  to deploy. `kubectl apply`, Argo CD sync, Helm install/upgrade/rollback,
  namespace or infrastructure changes, and secret creation/rotation require
  explicit authorization for the exact target and environment.
- Never put kubeconfig material, secret values, tokens, webhook URLs, or
  rendered secret payloads in a commit, report, command output, or log.

## Required workflow

Before changing a deployment file, read the whole file and identify its
consumer and target environment. Search the workflow/manifests that reference
it, reconcile that evidence with `docs/engineering/gotchas.md` and
`.github/workflows/README.md`, and record whether the path is live, legacy, or
unresolved. Keep validation read-only and keep any later deploy, apply,
rollback, or secret operation as a separately authorized action.

The legacy chart has a narrow, read-only lint command:

```sh
helm lint deployment/helm/rag-system/
```

This checks only the legacy `deployment/helm/rag-system/` chart package. It
does not validate the live Argo CD path, which is
`infrastructure/helm/knowledge-graph-analytics` rendered with `values.yaml`
plus `values-aws.yaml`.

## Verification

Run the legacy lint above with the repository-compatible Helm binary when the
chart itself is in scope. A successful lint is syntax/chart evidence only; it
does not establish live reachability, cluster health, rollout success, or
authorization to mutate an environment. If the required binary or chart
consumer evidence is unavailable, report the check as `NOT RUN` or unresolved.
