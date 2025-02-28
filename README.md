> [!CAUTION]
> This repository was moved to https://github.com/ecmwf/downstream-ci. Do not update!

# Downstream CI Workflows

GitHub workflows to run CI for downstream repos.

Motivation is to have the dependency tree all in once place.

Split into trees for self-hosted runners and HPC.

Contains a build matrix of different platforms and compilers.

## Usage

When calling downstream CI, specify the ref for each package you need to test with, if it differs from the default value (usually `develop`). The triggerring repository always needs to pass the input for itself.

See the `inputs` section and https://docs.github.com/en/actions/using-workflows/reusing-workflows#calling-a-reusable-workflow.
Alternatively see one of the repositories which is already in the downstream CI.

## Dependency tree

Defines dependencies for each package to allow efficient caching. It's used to create the cache key by build-package and build-package-hpc to allow efficient caching.
