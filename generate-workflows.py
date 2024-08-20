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


def tree_get_package_var(
    var_name: str, dep_tree: dict, package: str, wf_name: str, default=None
):
    """Get package variable from dep tree. Prefers vars set for given workflow name."""
    wf_spec = dep_tree[package].get(wf_name, {})
    general = dep_tree[package]
    if wf_spec.get(var_name) is not None:
        return wf_spec[var_name]
    if general.get(var_name) is not None:
        return general[var_name]
    return default


def get_type_deps(
    package: str, dep_tree: dict, wf_name, type: Literal["cmake", "python"]
):
    package_deps = get_package_deps(package, dep_tree, wf_name)
    type_deps = []
    for dep in package_deps:
        if dep_tree[dep].get("type", "cmake") == type:
            type_deps.append(dep)
    return type_deps


def is_input(package, dep_tree, wf_name, wf_private) -> bool:
    is_input = tree_get_package_var("input", dep_tree, package, wf_name) is not False
    is_pkg_private = tree_get_package_var("private", dep_tree, package, wf_name, False)
    # add private packages as inputs to private wfs only
    # add non-private packages to all wfs
    return is_input and (is_pkg_private is False or is_pkg_private == wf_private)


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
    private: bool = False

    def add_job(self, job: Job):
        self.jobs[job.name] = job

    # generate inputs - runner type specific inputs + list of packages
    #   read config for specific inputs and dep tree for packages
    def generate_inputs(self, dep_tree: dict, wf_config: dict):
        wf_spec_inputs: dict = wf_config.get("inputs", {})
        self.inputs.update(wf_spec_inputs)

        for package, val in dep_tree.items():
            if is_input(package, dep_tree, self.name, self.private):
                self.inputs[package] = {"required": False, "type": "string"}

    def concurrency(self) -> object:
        c = {
            "group": "${{ github.workflow }}-${{ github.ref }}",
            "cancel-in-progress": True,
        }

        return c
    
    def __getstate__(self) -> object:
        d = {
            "name": self.name,
            "on": {"workflow_call": {"inputs": self.inputs}},
            "concurrency": self.concurrency(),
            "jobs": self.jobs,
        }
        if self.private:
            dispatch_type = (
                "downstream-ci"
                if self.wf_type == "build-package"
                else "downstream-ci-hpc"
            )
            d["on"]["repository_dispatch"] = {"types": [dispatch_type]}

        return d

    def get_prepare_inputs_script(self, pkgs) -> str:
        lines = []
        if self.private:
            for pkg in pkgs:
                lines.append(
                    f'echo {pkg}="'
                    + "${{ "
                    + f"inputs.{pkg} || github.event.client_payload.inputs.{pkg}"
                    + ' }}" >> $GITHUB_OUTPUT'
                )
        else:
            for pkg in pkgs:
                lines.append(
                    f'echo {pkg}="'
                    + "${{ "
                    + f"inputs.{pkg}"
                    + ' }}" >> $GITHUB_OUTPUT'
                )
        return lines

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
                "run": (
                    "python -m pip install --upgrade pip\n"
                    "python -m pip install black flake8 isort\n"
                ),
            },
            {"name": "Check isort", "run": "isort --check . --profile black"},
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

    def add_clang_format_job(self):
        self.inputs["clang_format"] = {
            "description": "Whether to run clang-format QA.",
            "type": "boolean",
            "required": False,
        }
        self.inputs["clang_format_ignore"] = {
            "description": "A list of paths to be skipped during formatting check.",
            "type": "string",
            "required": False,
        }

        steps = r"""
                - name: Checkout repository
                  uses: actions/checkout@v4

                - name: Install clang-format
                  run: |
                    wget -O - https://apt.llvm.org/llvm-snapshot.gpg.key | sudo apt-key add -
                    sudo add-apt-repository deb http://apt.llvm.org/jammy/ llvm-toolchain-jammy-16 main
                    sudo apt update
                    sudo apt install -y clang-format-16

                - name: Run clang-format
                  shell: bash {0}
                  run: |
                    ignore="./\($(echo "${{ inputs.clang_format_ignore }}" | sed ':a;N;$!ba;s/\n/\\|/g')\)"
                    echo "Ignore: $ignore"
                    files=$(find . -not \( -regex $ignore -prune \) -regex ".*\.\(cpp\|hpp\|cc\|cxx\|h\|c\)")
                    errors=0

                    if [ ! -e ".clang-format" ]
                    then
                        echo "::error::Missing .clang-format file"
                        exit 1
                    fi

                    for file in $files; do
                        clang-format-16 --dry-run --Werror --style=file --fallback-style=none $file
                        if [ $? -ne 0 ]; then
                            ((errors++))
                        fi
                    done

                    if [ $errors -ne 0 ]; then
                        echo "::error::clang-format failed for $errors files"
                        exit 1
                    fi"""
        self.add_job(
            Job(
                name="clang-format",
                needs=["setup"],
                condition="${{ inputs.clang_format }}",
                steps=yaml.safe_load(steps),
            )
        )

    def generate_package_jobs(self, dep_tree: dict):
        for package, pkg_conf in dep_tree.items():
            if not is_input(package, dep_tree, self.name, self.private):
                continue
            if self.private != tree_get_package_var(
                "private", dep_tree, package, self.name, False
            ):
                continue
            package_deps = get_package_deps(package, dep_tree, self.name)
            cmake_deps = [
                "${{ " + f"needs.setup.outputs.{dep}" + " }}"
                for dep in get_type_deps(package, dep_tree, self.name, "cmake")
                if is_input(dep, dep_tree, self.name, self.private)
            ]
            python_deps = [
                "${{ " + f"needs.setup.outputs.{dep}" + " }}"
                for dep in get_type_deps(package, dep_tree, self.name, "python")
                if is_input(dep, dep_tree, self.name, self.private)
            ]
            needs = [
                dep
                for dep in package_deps
                if is_input(dep, dep_tree, self.name, self.private)
                and self.private
                == tree_get_package_var("private", dep_tree, dep, self.name, False)
            ]
            condition_inputs = " || ".join(
                [
                    f"needs.setup.outputs.{dep}"
                    for dep in package_deps
                    if is_input(dep, dep_tree, self.name, self.private)
                ]
                + [f"needs.setup.outputs.{package}"]
            )
            needs.append("setup")

            condition = (
                "${{ (always() && !cancelled()) "
                "&& contains(join(needs.*.result, ','), 'success') "
                f"&& needs.setup.outputs.{package}_matrix "
                f"&& ({condition_inputs})"
                " }}"
            )
            strategy = {
                "fail-fast": False,
                "matrix": "${{ "
                + f"fromJson(needs.setup.outputs.{package}_matrix)"
                + " }}",
            }
            runs_on = "${{ matrix.labels }}"
            package_env = tree_get_package_var("env", dep_tree, package, self.name)
            env = {"DEP_TREE": "${{ needs.setup.outputs.dep_tree }}"}
            env.update(package_env) if package_env else None
            test_cmd = tree_get_package_var("test_cmd", dep_tree, package, self.name)
            mkdir = tree_get_package_var("mkdir", dep_tree, package, self.name) or []
            conda_deps = tree_get_package_var(
                "conda_deps", dep_tree, package, self.name
            )
            build_package_python = tree_get_package_var(
                "build-package-python", dep_tree, package, self.name
            )
            steps = []
            if self.wf_type == "build-package":
                if pkg_conf.get("type", "cmake") == "cmake":
                    needs.append("clang-format")
                    s = {
                        "uses": (
                            "ecmwf-actions/reusable-workflows/"
                            "build-package-with-config@v2"
                        ),
                        "with": {
                            "repository": "${{ matrix.owner_repo_ref }}",
                            "build_package_inputs": (
                                "repository: ${{ matrix.owner_repo_ref }}"
                            ),
                            "build_config": "${{ matrix.config_path }}",
                            "build_dependencies": "\n".join(cmake_deps),
                        },
                    }
                    if not self.private:
                        s["with"][
                            "codecov_token"
                        ] = "${{ secrets.CODECOV_UPLOAD_TOKEN }}"
                        s["with"]["codecov_upload"] = (
                            "${{ needs.setup.outputs.trigger_repo "
                            "== github.job && inputs.codecov_upload }}"
                        )
                    else:
                        s["with"]["github_token"] = "${{ secrets.GH_REPO_READ_TOKEN }}"
                    if build_package_python:
                        s["with"]["python_version"] = build_package_python
                    steps.append(s)
                if pkg_conf.get("type", "cmake") == "python":
                    needs.append("python-qa")
                    if len(cmake_deps):
                        # python package with cmake deps
                        s = {
                            "name": "Build dependencies",
                            "id": "build-deps",
                            "uses": (
                                "ecmwf-actions/reusable-workflows/"
                                "build-package-with-config@v2"
                            ),
                            "with": {
                                "repository": "${{ matrix.owner_repo_ref }}",
                                "codecov_upload": False,
                                "build_package_inputs": (
                                    "repository: ${{ matrix.owner_repo_ref }}"
                                ),
                                "build_config": "${{ matrix.config_path }}",
                                "build_dependencies": "\n".join(cmake_deps),
                            },
                        }
                        if self.private:
                            s["with"]["github_token"] = "${{ secrets.GH_REPO_READ_TOKEN }}"
                        if build_package_python:
                            s["with"]["python_version"] = build_package_python
                        steps.append(s)
                        for path in mkdir:
                            steps.append({"run": f"mkdir -p {path}"})
                        ci_python_step = {
                            "uses": "ecmwf-actions/reusable-workflows/ci-python@v2",
                            "with": {
                                "lib_path": (
                                    "${{ steps.build-deps.outputs.lib_path }}"
                                ),
                                "bin_paths": (
                                    "${{ steps.build-deps.outputs.bin_paths }}"
                                ),
                                "python_dependencies": "\n".join(python_deps),
                            },
                        }
                        if pkg_conf.get("requirements_path"):
                            ci_python_step["with"]["requirements_path"] = pkg_conf.get(
                                "requirements_path"
                            )
                        if pkg_conf.get("toml_opt_dep_sections"):
                            ci_python_step["with"]["toml_opt_dep_sections"] = (
                                pkg_conf.get("toml_opt_dep_sections")
                            )
                        if test_cmd:
                            ci_python_step["with"]["test_cmd"] = test_cmd
                        if conda_deps:
                            ci_python_step["with"]["conda_install"] = conda_deps
                        if not self.private:
                            ci_python_step["with"]["codecov_upload"] = (
                                "${{ needs.setup.outputs.trigger_repo == "
                                "github.job && inputs.codecov_upload "
                                "&& needs.setup.outputs.py_codecov_platform "
                                "== matrix.name }}"
                            )
                            ci_python_step["with"][
                                "codecov_token"
                            ] = "${{ secrets.CODECOV_UPLOAD_TOKEN }}"
                        else:
                            ci_python_step["with"]["github_token"] = "${{ secrets.GH_REPO_READ_TOKEN }}"
                        steps.append(ci_python_step)
                    else:
                        # pure python package
                        ci_python_step = {
                            "uses": "ecmwf-actions/reusable-workflows/ci-python@v2",
                            "with": {
                                "repository": "${{ matrix.owner_repo_ref }}",
                                "checkout": True,
                                "python_dependencies": "\n".join(python_deps),
                            },
                        }
                        if pkg_conf.get("requirements_path"):
                            ci_python_step["with"]["requirements_path"] = pkg_conf.get(
                                "requirements_path"
                            )
                        if pkg_conf.get("toml_opt_dep_sections"):
                            ci_python_step["with"]["toml_opt_dep_sections"] = (
                                pkg_conf.get("toml_opt_dep_sections")
                            )
                        if test_cmd:
                            ci_python_step["with"]["test_cmd"] = test_cmd
                        if conda_deps:
                            ci_python_step["with"]["conda_install"] = conda_deps
                        if not self.private:
                            ci_python_step["with"]["codecov_upload"] = (
                                "${{ needs.setup.outputs.trigger_repo == "
                                "github.job && inputs.codecov_upload "
                                "&& needs.setup.outputs.py_codecov_platform "
                                "== matrix.name }}"
                            )
                            ci_python_step["with"][
                                "codecov_token"
                            ] = "${{ secrets.CODECOV_UPLOAD_TOKEN }}"
                        else:
                            ci_python_step["with"]["github_token"] = "${{ secrets.GH_REPO_READ_TOKEN }}"
                        steps.append(ci_python_step)
            if self.wf_type == "build-package-hpc":
                runs_on = [
                    "self-hosted",
                    "linux",
                    "hpc",
                ]
                s = {
                    "uses": "ecmwf-actions/reusable-workflows/ci-hpc@v2",
                    "with": {
                        "github_user": ("${{ secrets.BUILD_PACKAGE_HPC_GITHUB_USER }}"),
                        "github_token": "${{ secrets.GH_REPO_READ_TOKEN }}",
                        "troika_user": "${{ secrets.HPC_CI_SSH_USER }}",
                        "repository": "${{ matrix.owner_repo_ref }}",
                        "build_config": "${{ matrix.config_path }}",
                        "dependencies": "\n".join(cmake_deps),
                        "python_dependencies": "\n".join(python_deps),
                    },
                }
                if pkg_conf.get("requirements_path"):
                    s["with"]["python_requirements"] = pkg_conf.get("requirements_path")
                if pkg_conf.get("toml_opt_dep_sections"):
                    s["with"]["python_toml_opt_dep_sections"] = pkg_conf.get(
                        "toml_opt_dep_sections"
                    )
                if conda_deps:
                    s["with"]["conda_deps"] = conda_deps
                steps.append(s)
            self.add_job(Job(package, needs, condition, strategy, env, runs_on, steps))

    def generate_setup_job(self, dep_tree: dict, wf_config: dict):
        outputs = {}
        deps = [
            dep for dep in dep_tree if is_input(dep, dep_tree, self.name, self.private)
        ]
        for dep in deps:
            outputs[dep] = "${{ " + f"steps.prepare-inputs.outputs.{dep}" + " }}"
            outputs[f"{dep}_matrix"] = "${{ " + f"steps.setup.outputs.{dep}" + " }}"

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
        if self.private:
            steps.append(
                {
                    "name": "${{ github.event.client_payload.id }}",
                    "if": "${{ github.event_name == 'repository_dispatch' }}",
                    "run": 'echo "Workflow ID ${{ github.event.client_payload.id }}"',
                }
            )
        steps.append(
            {
                "name": "Prepare inputs",
                "id": "prepare-inputs",
                "run": "\n".join(self.get_prepare_inputs_script(deps)),
            }
        )
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
            if is_input(dep, dep_tree, self.name, self.private):
                config_path = tree_get_package_var(
                    "config_path", dep_tree, dep, self.name
                )
                if config_path is None:
                    config_path = default_config_path

                setup_config[f"ecmwf/{dep}"] = {
                    "path": config_path,
                    "python": dep_tree[dep].get("type", "cmake") == "python",
                    "master_branch": tree_get_package_var(
                        "master_branch", dep_tree, dep, self.name
                    )
                    or "master",
                    "develop_branch": tree_get_package_var(
                        "develop_branch", dep_tree, dep, self.name
                    )
                    or "develop",
                    "input": "${{ " + f"steps.prepare-inputs.outputs.{dep}" + " }}",
                    "optional_matrix": tree_get_package_var(
                        "optional_matrix", dep_tree, dep, self.name
                    ),
                }

        s = {
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
                "PYTHON_VERSIONS": yaml.dump(
                    wf_config["python_versions"], indent=2, default_flow_style=False
                )
                + "\n",
                "PYTHON_JOBS": yaml.dump(
                    wf_config.get("python_jobs", []),
                    indent=2,
                    default_flow_style=False,
                )
                + "\n",
                "MATRIX": yaml.dump(wf_config["matrix"], indent=2),
                "OPTIONAL_MATRIX": yaml.dump(
                    wf_config["optional_matrix"], indent=2, default_flow_style=False
                ),
            },
            "run": "python setup_downstream_ci.py",
        }

        s["env"]["SKIP_MATRIX_JOBS"] = (
            (
                "${{ inputs.skip_matrix_jobs || github.event.client_payload.inputs."
                "skip_matrix_jobs}}"
            )
            if self.private
            else "${{ inputs.skip_matrix_jobs }}"
        )
        steps.append(s)
        self.add_job(Job("setup", steps=steps, outputs=outputs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Path to configuration file", required=True)
    parser.add_argument(
        "--dep-tree", help="Path to dependency tree file.", required=True
    )
    parser.add_argument(
        "--output",
        help=(
            "Path to output directory. "
            "Workflow files will be created/overwritten there."
        ),
        required=True,
    )
    parser.add_argument("workflows", nargs="*")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config: dict = yaml.safe_load(f)

    with open(args.dep_tree, "r") as f:
        dep_tree: dict = yaml.safe_load(f)

    for name in config.keys():
        if args.workflows and name not in args.workflows:
            continue
        wf = Workflow(
            name=name,
            wf_type=config[name]["type"],
            private=config[name].get("private", False),
        )
        wf.generate_inputs(dep_tree, config[name])
        wf.generate_setup_job(dep_tree, config[name])
        if config[name].get("python_qa", False):
            wf.add_python_qa_job()
        if config[name].get("clang_format", False):
            wf.add_clang_format_job()
        wf.generate_package_jobs(dep_tree)
        print(yaml.dump(wf, indent=2, sort_keys=False, default_flow_style=False))
        print("=" * 10)
        with open(PurePath(args.output, name + ".yml"), "w") as f:
            f.write(
                f"{'#\n'*3}# This is a file generated by generate-workflows.py - DO NOT EDIT!!\n{'#\n'*3}"
            )
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
