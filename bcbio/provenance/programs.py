"""Identify program versions used for analysis, reporting in structured table.

Catalogs the full list of programs used in analysis, enabling reproduction of
results and tracking of provenance in output files.
"""
import os
import collections
import subprocess

from bcbio import broad, utils
from bcbio.pipeline import config_utils, version

_cl_progs = [{"cmd": "bamtools", "args": "--version", "stdout_flag": "bamtools"},
             {"cmd": "bedtools", "args": "--version", "stdout_flag": "bedtools"},
             {"cmd": "bowtie2", "args": "--version", "stdout_flag": "bowtie2-align"},
             {"cmd": "bwa", "stdout_flag": "Version:"},
             {"cmd": "freebayes", "stdout_flag": "version:"},
             {"cmd": "novosort", "paren_flag": "novosort"},
             {"cmd": "novoalign", "stdout_flag": "Novoalign"},
             {"cmd": "samtools", "stdout_flag": "Version"}]
# TODO: ogap, bamleftalign

def _broad_versioner(type):
    def get_version(config):
        runner = broad.runner_from_config(config)
        if type == "gatk":
            return runner.get_gatk_version()
        elif type == "picard":
            return runner.get_picard_version("ViewSam")
        else:
            raise NotImplementedError(type)
    return get_version

_alt_progs = [{"name": "gatk", "version_fn": _broad_versioner("gatk")},
              {"name": "picard", "version_fn": _broad_versioner("picard")}]
# TODO: varscan, bcbio.variation, cortex_var

def _parse_from_stdoutflag(stdout, x):
    for line in stdout:
        if line.find(x) >= 0:
            return line.split()[-1]
    return ""

def _parse_from_parenflag(stdout, x):
    for line in stdout:
        if line.find(x) >= 0:
            return line.split("(")[-1].split(")")[0]
    return ""

def _get_cl_version(p, config):
    """Retrieve version of a single commandline program.
    """
    prog = config_utils.get_program(p["cmd"], config)
    args = p.get("args", "")

    cmd = "{prog} {args}"
    subp = subprocess.Popen(cmd.format(**locals()), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            shell=True)
    if p.get("stdout_flag"):
        version = _parse_from_stdoutflag(subp.stdout, p["stdout_flag"])
    elif p.get("paren_flag"):
        version = _parse_from_parenflag(subp.stdout, p["paren_flag"])
    else:
        print p.stdout.read()
        raise NotImplementedError("Don't know how to extract version")
    return version

def get_versions(config):
    """Retrieve details on all programs available on the system.
    """
    out = [{"program": "bcbio-nextgen",
            "version": version.__version__}]
    for p in _cl_progs:
        out.append({"program": p["cmd"],
                    "version": _get_cl_version(p, config)})
    for p in _alt_progs:
        out.append({"program": p["name"],
                    "version": p["version_fn"](config)})
    return out

def write_versions(dirs, config):
    """Write CSV file with versions used in analysis pipeline.
    """
    base_dir = utils.safe_makedir(os.path.join(dirs["work"], "provenance"))
    out_file = os.path.join(base_dir, "programs.txt")
    with open(out_file, "w") as out_handle:
        for p in get_versions(config):
            out_handle.write("{program},{version}\n".format(**p))
    return out_file