"""
The purpose of this script is to setup the data needed for downstream CI

Inputs (as environment variables):
    TOKEN: Github token
    CONFIG: Yaml object, which contains paths to build config file for all repos
            in downstream ci. Optionally whether it's a python package (can be
            ommitted). example:
            ```
            owner/repo@main:
              path: .github/ci-config.yml
              python: true
            ```
    SKIP_MATRIX_JOBS: Multiline string, list of matrix job names to be skipped 
    PYTHON_VERSIONS: Yaml list, list of python version to expand the matrix with
    PYTHON_JOBS: Yaml list, list of jobs to be used for python packages
    MATRIX: Yaml object, see
            https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#jobsjob_idstrategymatrix

Outputs:
    Outputs are written to $GITHUB_OUTPUT file.

    trigger_repo: name of the triggerring repository without the owner prefix
    build_package_dep_tree: parsed dependency tree in yaml format for build-package
    build_package_hpc_dep_tree: parsed dependency tree in yaml format for 
                                build-package-hpc
    <repo>: for each repo in CONFIG input, this will produce an output with name of the
            repository. Contains the build
            matrix for the specific package.
"""


import json
import os
import sys

import requests
import yaml

# Load inputs
ci_config: dict = yaml.safe_load(os.getenv("CONFIG", ""))
python_versions = yaml.safe_load(os.getenv("PYTHON_VERSIONS", ""))
python_jobs = yaml.safe_load(os.getenv("PYTHON_JOBS", ""))
matrix = yaml.safe_load(os.getenv("MATRIX", ""))
skip_jobs = os.getenv("SKIP_MATRIX_JOBS", "").splitlines()
token = os.getenv("TOKEN", "")
trigger_ref_name = os.getenv("GITHUB_REF_NAME", "")

github_repository = os.getenv("GITHUB_REPOSITORY", "")
_, trigger_repo = github_repository.split("/")
print(f"Triggered from: {trigger_repo}")


DEFAULT_MASTER_BRANCH_NAME = "master"
DEFAULT_DEVELOP_BRANCH_NAME = "develop"


# Get build-pacakge(-hpc) config for each repo
def get_config(owner, repo, ref, path):
    print(f"Getting config for {owner}/{repo}@{ref}")
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    response = requests.get(url, headers={"Authorization": f"token {token}"})
    return_obj = {"repo": repo, "matrix": [], "config_found": False}

    if response.status_code == 200:
        content = response.content.decode()
        config = yaml.safe_load(content)
        return_obj["matrix"] = config.get("matrix", [])
        return_obj["config_found"] = True
        return return_obj

    print(f"::warning::Config for {repo} not found.")
    print(response.status_code, response.content)

    if trigger_repo == return_obj["repo"]:
        print("::error::Config file for triggering repository not found")
        sys.exit(1)

    return return_obj


if skip_jobs:
    matrix["name"] = [name for name in matrix["name"] if name not in skip_jobs]
    matrix["include"] = [d for d in matrix["include"] if d["name"] not in skip_jobs]


matrices = {}

# whether to use master branch for dependencies
# if triggered from master branch (as defined in the config)
use_master = (
    ci_config[github_repository].get("master_branch", DEFAULT_MASTER_BRANCH_NAME)
    == trigger_ref_name
)

for owner_repo, val in ci_config.items():
    owner_repo: str
    val: dict

    owner, repo = owner_repo.split("/")

    master_branch_name = val.get("master_branch", DEFAULT_MASTER_BRANCH_NAME)
    develop_branch_name = val.get("develop_branch", DEFAULT_DEVELOP_BRANCH_NAME)
    ref = master_branch_name if use_master else develop_branch_name
    package_input = val.get("input", "")
    if package_input:
        _, ref = package_input.split("@")

    path = val.get("path", "")
    config = get_config(owner, repo, ref, path)

    if not config["config_found"]:
        continue

    if config["matrix"]:
        matrices[repo] = {**matrix, "config": config["matrix"]}
    else:
        matrices[repo] = {**matrix}

    for index, item in enumerate(matrices[repo]["include"]):
        matrices[repo]["include"][index]["owner_repo_ref"] = f"{owner}/{repo}@{ref}"
        matrices[repo]["include"][index]["config_path"] = path

    if val.get("python", ""):
        matrices[repo]["python_version"] = python_versions
        if python_jobs:
            matrices[repo]["name"] = [
                name for name in matrices[repo]["name"] if name in python_jobs
            ]
            matrices[repo]["include"] = [
                d for d in matrices[repo]["include"] if d["name"] in python_jobs
            ]


print("Build matrices:")
yaml.Dumper.ignore_aliases = lambda *args: True
print(yaml.dump(matrices, sort_keys=False))


with open("dependency_tree.yml", "r") as f:
    dep_tree = list(yaml.safe_load_all(f))

build_package_dep_tree: dict = [
    d for d in dep_tree if d.get("name") == "build-package"
][0]
build_package_dep_tree.pop("name", None)
build_package_hpc_dep_tree: dict = [
    d for d in dep_tree if d.get("name") == "build-package-hpc"
][0]
build_package_hpc_dep_tree.pop("name", None)

print(
    "build-package dependency tree:\n",
    yaml.dump(build_package_dep_tree, sort_keys=False),
)
print(
    "build-package-hpc dependency tree:\n",
    yaml.dump(build_package_hpc_dep_tree, sort_keys=False),
)

with open(os.getenv("GITHUB_OUTPUT"), "a") as f:
    print("trigger_repo", trigger_repo, sep="=", file=f)

    print("build_package_dep_tree<<EOF", file=f)
    print(yaml.dump(build_package_dep_tree), file=f)
    print("EOF", file=f)

    print("build_package_hpc_dep_tree<<EOF", file=f)
    print(yaml.dump(build_package_hpc_dep_tree), file=f)
    print("EOF", file=f)

    for key, value in matrices.items():
        print(f"{key}<<EOF", file=f)
        print(json.dumps(value, separators=(",", ":")), file=f)
        print("EOF", file=f)
