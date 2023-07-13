import json
import os
import sys

import requests
import yaml

# Load inputs
config_paths: dict = yaml.safe_load(os.getenv("CONFIG_PATHS", ""))
ci_config: dict = yaml.safe_load(os.getenv("CONFIG", ""))
python_versions = yaml.safe_load(os.getenv("PYTHON_VERSIONS", ""))
python_jobs = yaml.safe_load(os.getenv("PYTHON_JOBS", ""))
matrix = yaml.safe_load(os.getenv("MATRIX", ""))
skip_jobs = os.getenv("SKIP_MATRIX_JOBS", "").splitlines()
token = os.getenv("TOKEN", "")

github_repository = os.getenv("GITHUB_REPOSITORY", "")
_, trigger_repo = github_repository.split("/")
print(f"Triggered from: {trigger_repo}")

if not ci_config:
    ci_config = config_paths


# Get config for each repo
def get_config(repo, path):
    print("Getting config for", repo)
    owner_repo, ref = repo.split("@")
    owner, repo = owner_repo.split("/")
    url = f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"
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

for k, v in ci_config.items():
    path = v["path"] if type(v) == dict else v
    config = get_config(k, path)

    if not config["config_found"]:
        continue

    if config["matrix"]:
        matrices[config["repo"]] = {**matrix, "config": config["matrix"]}
    else:
        matrices[config["repo"]] = {**matrix}

    if type(v) == dict and v.get("python", ""):
        matrices[config["repo"]]["python_version"] = python_versions
        if python_jobs:
            matrices[config["repo"]]["name"] = [
                name for name in matrices[config["repo"]]["name"] if name in python_jobs
            ]
            matrices[config["repo"]]["include"] = [
                d
                for d in matrices[config["repo"]]["include"]
                if d["name"] in python_jobs
            ]


print("Build matrices:")
yaml.Dumper.ignore_aliases = lambda *args: True
print(yaml.dump(matrices, sort_keys=False))


with open("dependency_tree.yml", "r") as f:
    dep_tree = list(yaml.safe_load_all(f))

build_package_dep_tree = [d for d in dep_tree if d.get("name") == "build-package"][0]
build_package_hpc_dep_tree = [
    d for d in dep_tree if d.get("name") == "build-package-hpc"
][0]


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
