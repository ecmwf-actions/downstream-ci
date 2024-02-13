#!/usr/bin/env python

import argparse
from pathlib import PurePath
from typing import Literal
import yaml

from dataclasses import dataclass, field


# modify how pyyaml dumps multiline strings - we want `|`
def str_presenter(dumper, data):
    if len(data.splitlines()) > 1:  # check for multiline string
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_presenter)
yaml.emitter.Emitter.prepare_tag = lambda self, tag: ""


def get_package_deps(
    package: str, dep_tree: dict, wf_name: str, deps: list[str] = None
):
    if deps is None:
        deps = []
    if package not in dep_tree or dep_tree[package] is None:
        return deps

    direct_deps = (
        dep_tree[package].get(wf_name, {}).get("deps")
        or dep_tree[package].get("deps")
        or []
    )

    for dep in direct_deps:
        if dep in deps:
            deps.remove(dep)
        deps.append(dep)
        if dep in dep_tree:
            get_package_deps(dep, dep_tree, wf_name, deps)

    return deps


def tree_get_package_var(var_name: str, dep_tree: dict, package: str, wf_name: str):
    """Get package variable from dep tree. Prefers vars set for given workflow name."""
    package_conf = dep_tree[package].get(wf_name) or dep_tree[package]
    return package_conf.get(var_name)


def get_type_deps(
    package: str, dep_tree: dict, wf_name, type: Literal["cmake", "python"]
):
    package_deps = get_package_deps(package, dep_tree, wf_name)
    type_deps = []
    for dep in package_deps:
        if dep_tree[dep].get("type", "cmake") == type:
            type_deps.append(dep)
    return type_deps


@dataclass
class Job:
    name: str
    needs: str | list[str] = None
    condition: str = None
    strategy: dict = None
    env: dict = None
    runs_on: str | list[str] = "ubuntu-latest"
    steps: list[dict] = field(default_factory=list)
    outputs: dict = None

    def __getstate__(self) -> object:
        d = {"name": self.name}
        if self.needs:
            d["needs"] = self.needs
        if self.condition:
            d["if"] = self.condition
        if self.strategy:
            d["strategy"] = self.strategy
        if self.env:
            d["env"] = self.env
        d["runs-on"] = self.runs_on
        if self.outputs:
            d["outputs"] = self.outputs
        d["steps"] = self.steps

        return d


@dataclass
class Workflow:
    name: str
    wf_type: Literal["build-package", "build-package-hpc"]
    inputs: dict = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)

    def add_job(self, job: Job):
        self.jobs[job.name] = job

    # generate inputs - runner type specific inputs + list of packages
    #   read config for specific inputs and dep tree for packages
    def generate_inputs(self, dep_tree: dict, wf_config: dict):
        wf_spec_inputs: dict = wf_config.get("inputs", {})
        self.inputs.update(wf_spec_inputs)

        for package, val in dep_tree.items():
            if tree_get_package_var("input", dep_tree, package, self.name) is not False:
                self.inputs[package] = {"required": False, "type": "string"}

    def __getstate__(self) -> object:
        d = {
            "name": self.name,
            "on": {"workflow_call": {"inputs": self.inputs}},
            "jobs": self.jobs,
        }
        return d

    def add_python_qa_job(self):
        self.inputs["python_qa"] = {
            "description": "Whether to run code QA tasks.",
            "type": "boolean",
            "required": False,
        }

        steps = [
            {
                "name": "Checkout Repository",
                "uses": "actions/checkout@v4",
                "with": {
                    "repository": "${{ inputs.repository }}",
                    "ref": "${{ inputs.ref }}",
                },
            },
            {
                "name": "Setup Python",
                "uses": "actions/setup-python@v4",
                "with": {"python-version": "3.x"},
            },
            {
                "name": "Install Python Dependencies",
                "run": "python -m pip install --upgrade pip\npython -m pip install black flake8 isort\n",
            },
            {"name": "Check isort", "run": "isort --check ."},
            {"name": "Check black", "run": "black --check ."},
            {"name": "Check flake8", "run": "flake8 ."},
        ]

        job = Job(
            name="python-qa",
            needs=["setup"],
            condition="${{ inputs.python_qa }}",
            steps=steps,
        )
        self.add_job(job)

    def generate_package_jobs(self, dep_tree: dict):
        for package, pkg_conf in dep_tree.items():
            if tree_get_package_var("input", dep_tree, package, self.name) is False:
                continue
            package_deps = get_package_deps(package, dep_tree, self.name)
            cmake_deps = get_type_deps(package, dep_tree, self.name, "cmake")
            python_deps = get_type_deps(package, dep_tree, self.name, "python")
            needs = [
                dep
                for dep in package_deps
                if tree_get_package_var("input", dep_tree, dep, self.name) is not False
            ]
            condition_inputs = " || ".join(
                [f"inputs.{dep}" for dep in needs + [package]]
            )
            build_deps = "\n".join(["${{ " + f"inputs.{dep}" + " }}" for dep in needs])
            needs.append("setup")

            condition = (
                "${{ (always() && !cancelled()) "
                "&& contains(join(needs.*.result, ','), 'success') "
                f"&& needs.setup.outputs.{package} "
                f"&& ({condition_inputs})"
                " }}"
            )
            strategy = {
                "fail-fast": False,
                "matrix": "${{ " + f"fromJson(needs.setup.outputs.{package})" + " }}",
            }
            runs_on = "${{ matrix.labels }}"
            env = {"DEP_TREE": "${{ needs.setup.outputs.dep_tree }}"}
            steps = []
            if self.wf_type == "build-package":
                if pkg_conf.get("type", "cmake") == "cmake":
                    steps.append(
                        {
                            "uses": "ecmwf-actions/reusable-workflows/build-package-with-config@v2",
                            "with": {
                                "repository": "${{ matrix.owner_repo_ref }}",
                                "codecov_upload": "${{ needs.setup.outputs.trigger_repo == github.job && inputs.codecov_upload }}",
                                "build_package_inputs": "repository: ${{ matrix.owner_repo_ref }}",
                                "build_config": "${{ matrix.config_path }}",
                                "build_dependencies": build_deps,
                            },
                        }
                    )
                if pkg_conf.get("type", "cmake") == "python":
                    needs.append("python-qa")
                    if len(cmake_deps):
                        # python package with cmake deps
                        steps.append(
                            {
                                "name": "Build dependencies",
                                "id": "build-deps",
                                "uses": "ecmwf-actions/reusable-workflows/build-package-with-config@v2",
                                "with": {
                                    "repository": "${{ matrix.owner_repo_ref }}",
                                    "codecov_upload": "${{ needs.setup.outputs.trigger_repo == github.job && inputs.codecov_upload }}",
                                    "build_package_inputs": "repository: ${{ matrix.owner_repo_ref }}",
                                    "build_config": "${{ matrix.config_path }}",
                                    "build_dependencies": "\n".join(
                                        [
                                            "${{ " + f"inputs.{dep}" + " }}"
                                            for dep in cmake_deps
                                            if tree_get_package_var(
                                                "input", dep_tree, dep, self.name
                                            )
                                            is not False
                                        ],
                                    ),
                                },
                            }
                        )
                        steps.append(
                            {
                                "uses": "ecmwf-actions/reusable-workflows/ci-python@v2",
                                "with": {
                                    "lib_path": "${{ steps.build-deps.outputs.lib_path }}",
                                    "conda_install": "libffi=3.3",
                                    "python_dependencies": "\n".join(
                                        [
                                            "${{ " + f"inputs.{dep}" + " }}"
                                            for dep in python_deps
                                            if tree_get_package_var(
                                                "input", dep_tree, dep, self.name
                                            )
                                            is not False
                                        ]
                                    ),
                                },
                            }
                        )
                    else:
                        # pure python package
                        steps.append(
                            {
                                "uses": "ecmwf-actions/reusable-workflows/ci-python@v2",
                                "with": {
                                    "repository": "${{ matrix.owner_repo_ref }}",
                                    "checkout": True,
                                    "python_dependencies": "\n".join(
                                        [f"inputs.{dep}" for dep in python_deps]
                                    ),
                                },
                            }
                        )
            if self.wf_type == "build-package-hpc":
                runs_on = "[self-hosted, linux, \"${{ inputs.dev_runner && 'hpc-dev' || 'hpc' }}\"]"
                steps.append(
                    {
                        "uses": "ecmwf-actions/reusable-workflows/ci-hpc@v2",
                        "with": {
                            "github_user": "${{ secrets.BUILD_PACKAGE_HPC_GITHUB_USER }}",
                            "github_token": "${{ secrets.GH_REPO_READ_TOKEN }}",
                            "troika_user": "${{ inputs.dev_runner && secrets.HPC_DEV_CI_SSH_USER || secrets.HPC_CI_SSH_USER }}",
                            "repository": "${{ matrix.owner_repo_ref }}",
                            "build_config": "${{ matrix.config_path }}",
                            "dependencies": "\n".join(
                                [f"inputs.{dep}" for dep in cmake_deps]
                            ),
                            "python_dependencies": "\n".join(
                                [f"inputs.{dep}" for dep in python_deps]
                            ),
                        },
                    }
                )
            self.add_job(Job(package, needs, condition, strategy, env, runs_on, steps))

    def generate_setup_job(self, dep_tree: dict, wf_config: dict):
        outputs = {}
        for dep in dep_tree:
            if tree_get_package_var("input", dep_tree, dep, self.name) is not False:
                outputs[dep] = "${{ " + f"steps.setup.outputs.{dep}" + " }}"
        if self.wf_type == "build-package":
            outputs["dep_tree"] = "${{ steps.setup.outputs.build_package_dep_tree }}"
            outputs["trigger_repo"] = "${{ steps.setup.outputs.trigger_repo }}"
            outputs["py_codecov_platform"] = (
                "${{ steps.setup.outputs.py_codecov_platform }}"
            )
        elif self.wf_type == "build-package-hpc":
            outputs["dep_tree"] = (
                "${{ steps.setup.outputs.build_package_hpc_dep_tree }}"
            )
        self.inputs.update(
            {
                "skip_matrix_jobs": {
                    "description": "List of matrix jobs to be skipped.",
                    "required": False,
                    "type": "string",
                }
            }
        )
        steps = []
        steps.append(
            {
                "name": "checkout reusable wfs repo",
                "uses": "actions/checkout@v4",
                "with": {"repository": "ecmwf-actions/downstream-ci", "ref": "main"},
            }
        )
        setup_config = {}
        default_config_path = (
            ".github/ci-config.yml"
            if self.wf_type == "build-package"
            else ".github/ci-hpc-config.yml"
        )
        for dep in dep_tree:
            if tree_get_package_var("input", dep_tree, dep, self.name) is not False:
                setup_config[f"ecmwf/{dep}"] = {
                    "path": tree_get_package_var(
                        "config_path", dep_tree, dep, self.name
                    )
                    or default_config_path,
                    "input": "${{ " + f"inputs.{dep}" + " }}",
                    "python": dep_tree[dep].get("type", "cmake") == "python",
                    "master_branch": tree_get_package_var(
                        "master_branch", dep_tree, dep, self.name
                    )
                    or "master",
                    "develop_branch": tree_get_package_var(
                        "develop_branch", dep_tree, dep, self.name
                    )
                    or "develop",
                }
        steps.append(
            {
                "name": "Run setup script",
                "id": "setup",
                "env": {
                    "TOKEN": "${{ secrets.GH_REPO_READ_TOKEN }}",
                    "CONFIG": yaml.dump(
                        setup_config,
                        indent=2,
                        default_flow_style=False,
                        sort_keys=False,
                    ),
                    "SKIP_MATRIX_JOBS": "${{ inputs.skip_matrix_jobs }}",
                    "PYTHON_VERSIONS": yaml.dump(
                        wf_config["python_versions"], indent=2, default_flow_style=False
                    )
                    + "\n",
                    "MATRIX": yaml.dump(wf_config["matrix"], indent=2),
                },
                "run": "python setup_downstream_ci.py",
            }
        )
        self.add_job(Job("setup", steps=steps, outputs=outputs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Path to configuration file", required=True)
    parser.add_argument(
        "--dep-tree", help="Path to dependency tree file.", required=True
    )
    parser.add_argument(
        "--output",
        help="Path to output directory. Workflow files will be created/overwritten there.",
        required=True,
    )
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config: dict = yaml.safe_load(f)

    with open(args.dep_tree, "r") as f:
        dep_tree: dict = yaml.safe_load(f)

    for name in config.keys():
        wf = Workflow(name=name, wf_type=config[name]["type"])
        wf.generate_inputs(dep_tree, config[name])
        wf.generate_setup_job(dep_tree, config[name])
        if config[name].get("python_qa", False):
            wf.add_python_qa_job()
        wf.generate_package_jobs(dep_tree)
        print(yaml.dump(wf, indent=2, sort_keys=False, default_flow_style=False))
        print("=" * 10)
        with open(PurePath(args.output, name + ".yml"), "w") as f:
            yaml.dump(
                wf,
                stream=f,
                indent=2,
                sort_keys=False,
                default_flow_style=False,
                width=float("inf"),
            )


if __name__ == "__main__":
    main()
