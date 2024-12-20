"""
The purpose of this script is to setup the data needed for downstream CI, mainly the
matrix and repository ref specific to each package.

Inputs (as environment variables):
    TOKEN: Github token
    CONFIG: Yaml object, which contains information for the setup script about packages
            in the downstream CI.
            Syntax:
            ```
            owner/repo:
                path: path/to/ci-config.yml     Required, from repository root
                python: true                    Optional, if python package
                input: ${{ inputs.package }}    Rquired, value from workflow inputs
                master_branch: branch1          Optional, name of master-type branch,
                                                default: "master"
                develop_branch: branch2         Optional, name of develop-type branch,
                                                default: "develop"
            ```
    SKIP_MATRIX_JOBS: Multiline string, list of matrix job names to be skipped
    PYTHON_VERSIONS: Yaml list, list of python version to expand the matrix with
    PYTHON_JOBS: Yaml list, list of jobs to be used for python packages
    MATRIX: Yaml object, see
            https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#jobsjob_idstrategymatrix
    OPTIONAL_MATRIX: same as $MATRIX, packages can opt in

Outputs:
    Outputs are written to $GITHUB_OUTPUT file.

    trigger_repo: name of the triggerring repository without the owner prefix
    build_package_dep_tree: parsed dependency tree in yaml format for build-package
    build_package_hpc_dep_tree: parsed dependency tree in yaml format for
                                build-package-hpc
    <repo>: for each repo in CONFIG input, this will produce an output with name of the
            repository. Contains the build matrix for the specific package. Matrix
            contains variables `owner_repo_ref` (used for repository input to
            build-package(-hpc)) and `config_path` (build config file path).
"""

import copy
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
optional_matrix = yaml.safe_load(os.getenv("OPTIONAL_MATRIX", "")) or {}
skip_jobs = os.getenv("SKIP_MATRIX_JOBS", "").splitlines()
token = os.getenv("TOKEN", "")
trigger_ref_name = os.getenv("DISPATCH_REF_NAME") or os.getenv("GITHUB_REF_NAME", "")
workflow_name = os.getenv("WORKFLOW_NAME", "")
ci_group = os.getenv("DOWNSTREAM_CI_GROUP", "")

github_repository = os.getenv("DISPATCH_REPOSITORY") or os.getenv(
    "GITHUB_REPOSITORY", ""
)
_, trigger_repo = github_repository.split("/")
print(f"Triggered from: {trigger_repo}")


DEFAULT_MASTER_BRANCH_NAME = "master"
DEFAULT_DEVELOP_BRANCH_NAME = "develop"

with open("dependency_tree.yml", "r") as f:
    dep_tree = yaml.safe_load(f)


def tree_get_package_var(var_name: str, dep_tree: dict, package: str, wf_name: str):
    """Get package variable from dep tree. Prefers vars set for given workflow name."""
    wf_spec = dep_tree[package].get(wf_name, {})
    general = dep_tree[package]
    if wf_spec.get(var_name) is not None:
        return wf_spec[var_name]
    if general.get(var_name) is not None:
        return general[var_name]
    return None


# Get build-pacakge(-hpc) config for each repo
def get_config(owner, repo, ref, path):
    print(f"Getting config for {owner}/{repo}@{ref}")
    return_obj = {"repo": repo, "matrix": [], "setup_matrix": False}
    if not path:
        print(f"Config path not provided for {repo}")
        return_obj["setup_matrix"] = True
        return return_obj

    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    response = requests.get(url, headers={"Authorization": f"token {token}"})

    if response.status_code == 200:
        content = response.content.decode()
        config = yaml.safe_load(content)
        return_obj["matrix"] = config.get("matrix", [])
        return_obj["setup_matrix"] = True
        return return_obj

    print(f"::warning::Config for {owner}/{repo}@{ref} not found.")
    print(response.status_code, response.content)

    if trigger_repo == return_obj["repo"]:
        print("::error::Config file for triggering repository not found")
        sys.exit(1)

    return return_obj


def get_ci_group_pkgs(ci_group: str, dep_tree: dict) -> list[str]:
    if not ci_group:
        ci_group = "all"

    print(f"CI group: {ci_group}")

    # Special cases
    # "all" -> all packages
    # "all_python" -> all python packages
    # "all_cmake" -> all cmake packages

    if ci_group == "all":
        return [k for k in dep_tree.keys()]
    if ci_group == "all_python":
        return [k for k, v in dep_tree.items() if v.get("type", "") == "python"]
    if ci_group == "all_cmake":
        return [k for k, v in dep_tree.items() if v.get("type", "") == "cmake"]

    with open("ci-groups.yml", "r") as f:
        ci_groups = yaml.safe_load(f)

    if ci_group in ci_groups:
        return ci_groups[ci_group]

    print(
        f"::error::CI group {ci_group} not found in "
        "ecmwf-actions/downstream-ci/ci-groups.yml"
    )
    sys.exit(1)


if skip_jobs:
    matrix["name"] = [name for name in matrix["name"] if name not in skip_jobs]
    matrix["include"] = [d for d in matrix["include"] if d["name"] not in skip_jobs]


matrices = {}
py_codecov_platform = ""

# whether to use master branch for dependencies
# if triggered from master branch (as defined in the config)
use_master = (
    ci_config.get(github_repository, {}).get(
        "master_branch", DEFAULT_MASTER_BRANCH_NAME
    )
    == trigger_ref_name
)
print("use_master: ", use_master)

for owner_repo, val in ci_config.items():
    owner, repo = owner_repo.split("/")

    master_branch_name = val.get("master_branch", DEFAULT_MASTER_BRANCH_NAME)
    develop_branch_name = val.get("develop_branch", DEFAULT_DEVELOP_BRANCH_NAME)
    ref = master_branch_name if use_master else develop_branch_name
    package_input = val.get("input", "")
    if package_input:
        _, ref = package_input.split("@")

    path = val.get("path", "")
    config = get_config(owner, repo, ref, path)

    if not config["setup_matrix"]:
        continue

    matrices[repo] = copy.deepcopy(matrix)

    for opt in optional_matrix.get("name", []):
        if val.get("optional_matrix", []) and opt in val.get("optional_matrix", []):
            matrices[repo]["name"].append(opt)
            matrices[repo]["include"].extend(
                [d for d in optional_matrix.get("include") if d["name"] == opt]
            )

    if config["matrix"]:
        matrices[repo]["config"] = config["matrix"]

    repo_skip = tree_get_package_var("skip", dep_tree, repo, workflow_name) or []
    if repo_skip:
        matrices[repo]["name"] = [
            name for name in matrices[repo]["name"] if name not in repo_skip
        ]
        matrices[repo]["include"] = [
            d for d in matrices[repo]["include"] if d["name"] not in repo_skip
        ]

    for index, item in enumerate(matrices[repo]["include"]):
        matrices[repo]["include"][index]["owner_repo_ref"] = f"{owner}/{repo}@{ref}"
        matrices[repo]["include"][index]["config_path"] = path

    if val.get("python", False) is True:
        matrices[repo]["python_version"] = python_versions
        if python_jobs:
            matrices[repo]["name"] = [
                name for name in matrices[repo]["name"] if name in python_jobs
            ]
            matrices[repo]["include"] = [
                d for d in matrices[repo]["include"] if d["name"] in python_jobs
            ]
        if repo == trigger_repo:
            py_codecov_platform = (
                matrices[repo]["name"][0] if len(matrices[repo]["name"]) else ""
            )


build_package_dep_tree = {}
build_package_hpc_dep_tree = {}


for package, conf in dep_tree.items():
    build_package_dep_tree[package] = {}
    if bp_deps := tree_get_package_var("deps", dep_tree, package, "downstream-ci"):
        build_package_dep_tree[package]["deps"] = bp_deps

    build_package_hpc_dep_tree[package] = {}
    if hpc_deps := tree_get_package_var("deps", dep_tree, package, "downstream-ci-hpc"):
        build_package_hpc_dep_tree[package]["deps"] = hpc_deps

    if hpc_modules := tree_get_package_var(
        "modules", dep_tree, package, "downstream-ci-hpc"
    ):
        build_package_hpc_dep_tree[package]["modules"] = hpc_modules


print("Build matrices:")
yaml.Dumper.ignore_aliases = lambda *args: True
print(yaml.dump(matrices, sort_keys=False))

print(
    "build-package dependency tree:\n",
    yaml.dump(build_package_dep_tree, sort_keys=False),
)
print(
    "build-package-hpc dependency tree:\n",
    yaml.dump(build_package_hpc_dep_tree, sort_keys=False),
)
print(f"Python codecov platform: {py_codecov_platform}")

ci_group_pkgs = get_ci_group_pkgs(ci_group, dep_tree)
print(f"CI group packages: {ci_group_pkgs}")

with open(os.getenv("GITHUB_OUTPUT"), "a") as f:
    print("trigger_repo", trigger_repo, sep="=", file=f)
    print("py_codecov_platform", py_codecov_platform, sep="=", file=f)
    print("use_master", use_master, sep="=", file=f)

    print("ci_group_pkgs<<EOF", file=f)
    print(json.dumps(ci_group_pkgs, separators=(",", ":")), file=f)
    print("EOF", file=f)

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
