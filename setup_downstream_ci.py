import json
import os

import requests
import yaml

# Load inputs
inputs = os.getenv("INPUTS")
config_paths: dict = yaml.safe_load(os.getenv("CONFIG_PATHS", ""))
matrix = yaml.safe_load(os.getenv("MATRIX", ""))
skip_jobs = os.getenv("SKIP_MATRIX_JOBS", "").splitlines()
token = os.getenv("TOKEN", "")


# Get config for each repo
def get_config(repo, path):
    print("Getting config for", repo)
    owner_repo, ref = repo.split("@")
    owner, repo = owner_repo.split("/")
    url = f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"
    response = requests.get(url, headers={"Authorization": f"token {token}"})
    if response.status_code == 200:
        content = response.content.decode()
        config = yaml.safe_load(content)

        return (repo, config.get("matrix", []))
    return


if skip_jobs:
    matrix["name"] = [name for name in matrix["name"] if name not in skip_jobs]
    matrix["include"] = [d for d in matrix["include"] if d["name"] not in skip_jobs]


matrices = {}

for k, v in config_paths.items():
    config = get_config(k, v)
    if config[1]:
        matrices[config[0]] = {**matrix, "config": config[1]}
    else:
        matrices[config[0]] = {**matrix}


print("Build matrices:")
yaml.Dumper.ignore_aliases = lambda *args: True
print(yaml.dump(matrices, sort_keys=False))

with open(os.getenv("GITHUB_OUTPUT"), "a") as f:
    for key, value in matrices.items():
        print(f"{key}<<EOF", file=f)
        print(json.dumps(value, separators=(",", ":")), file=f)
        print("EOF", file=f)
