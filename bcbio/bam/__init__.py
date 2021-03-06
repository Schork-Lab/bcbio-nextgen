"""Functionality to query and extract information from aligned BAM files.
"""
import collections
import contextlib
import os
import itertools
import subprocess
import numpy

import pysam
import toolz as tz

from bcbio import utils
from bcbio.bam import ref
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import config_utils
from bcbio.provenance import do

def is_paired(bam_file):
    """Determine if a BAM file has paired reads.
    """
    bam_file = utils.remote_cl_input(bam_file)
    cmd = ("sambamba view -h {bam_file} | head -50000 | "
           "sambamba view -S -F paired /dev/stdin  | head -1 | wc -l")
    out = subprocess.check_output(cmd.format(**locals()), shell=True,
                                  executable=do.find_bash(),
                                  stderr=open("/dev/null", "w"))
    return int(out) > 0

def index(in_bam, config):
    """Index a BAM file, skipping if index present.

    Centralizes BAM indexing providing ability to switch indexing approaches.
    """
    assert is_bam(in_bam), "%s in not a BAM file" % in_bam
    index_file = "%s.bai" % in_bam
    alt_index_file = "%s.bai" % os.path.splitext(in_bam)[0]
    if (not utils.file_uptodate(index_file, in_bam) and
          not utils.file_uptodate(alt_index_file, in_bam)):
        sambamba = _get_sambamba(config)
        samtools = config_utils.get_program("samtools", config)
        num_cores = config["algorithm"].get("num_cores", 1)
        with file_transaction(config, index_file) as tx_index_file:
            samtools_cmd = "{samtools} index {in_bam} {tx_index_file}"
            if sambamba:
                cmd = "{sambamba} index -t {num_cores} {in_bam} {tx_index_file}"
            else:
                cmd = samtools_cmd
            # sambamba has intermittent multicore failures. Allow
            # retries with single core
            try:
                do.run(cmd.format(**locals()), "Index BAM file: %s" % os.path.basename(in_bam),
                       log_error=False)
            except:
                do.run(samtools_cmd.format(**locals()),
                       "Index BAM file (single core): %s" % os.path.basename(in_bam))
    return index_file if utils.file_uptodate(index_file, in_bam) else alt_index_file

def idxstats(in_bam, data):
    """Return BAM index stats for the given file, using samtools idxstats.
    """
    index(in_bam, data["config"])
    AlignInfo = collections.namedtuple("AlignInfo", ["contig", "length", "aligned", "unaligned"])
    samtools = config_utils.get_program("samtools", data["config"])
    idxstats_out = subprocess.check_output([samtools, "idxstats", in_bam])
    out = []
    for line in idxstats_out.split("\n"):
        if line.strip():
            contig, length, aligned, unaligned = line.split("\t")
            out.append(AlignInfo(contig, int(length), int(aligned), int(unaligned)))
    return out

def get_downsample_pct(in_bam, target_counts, data):
    """Retrieve percentage of file to downsample to get to target counts.
    """
    total = sum(x.aligned for x in idxstats(in_bam, data))
    with contextlib.closing(pysam.Samfile(in_bam, "rb")) as work_bam:
        n_rgs = max(1, len(work_bam.header.get("RG", [])))
    rg_target = n_rgs * target_counts
    if total > rg_target:
        return float(rg_target) / float(total)

def get_aligned_reads(in_bam, data):
    index(in_bam, data["config"])
    bam_stats = idxstats(in_bam, data)
    align = sum(x.aligned for x in bam_stats)
    unaligned = sum(x.unaligned for x in bam_stats)
    total = float(align + unaligned)
    return 1.0 * align / total

def downsample(in_bam, data, target_counts, read_filter="", always_run=False,
               work_dir=None):
    """Downsample a BAM file to the specified number of target counts.
    """
    index(in_bam, data["config"])
    ds_pct = get_downsample_pct(in_bam, target_counts, data)
    if always_run and not ds_pct:
        ds_pct = 1.0
    if ds_pct:
        out_file = "%s-downsample%s" % os.path.splitext(in_bam)
        if work_dir:
            out_file = os.path.join(work_dir, os.path.basename(out_file))
        if not utils.file_exists(out_file):
            with file_transaction(data, out_file) as tx_out_file:
                sambamba = config_utils.get_program("sambamba", data["config"])
                num_cores = data["config"]["algorithm"].get("num_cores", 1)
                cmd = ("{sambamba} view -t {num_cores} {read_filter} -f bam -o {tx_out_file} "
                       "--subsample={ds_pct:.3} --subsampling-seed=42 {in_bam}")
                do.run(cmd.format(**locals()), "Downsample BAM file: %s" % os.path.basename(in_bam))
        return out_file

def check_header(in_bam, rgnames, ref_file, config):
    """Ensure passed in BAM header matches reference file and read groups names.
    """
    _check_bam_contigs(in_bam, ref_file, config)
    _check_sample(in_bam, rgnames)

def _check_sample(in_bam, rgnames):
    """Ensure input sample name matches expected run group names.
    """
    with contextlib.closing(pysam.Samfile(in_bam, "rb")) as bamfile:
        rg = bamfile.header.get("RG", [{}])
    msgs = []
    if len(rg) > 1:
        msgs.append("Multiple read groups found in input BAM. Expect single RG per BAM.")
    elif len(rg) == 0:
        msgs.append("No read groups found in input BAM. Expect single RG per BAM.")
    elif rg[0].get("SM") != rgnames["sample"]:
        msgs.append("Read group sample name (SM) does not match configuration `description`: %s vs %s"
                    % (rg[0].get("SM"), rgnames["sample"]))
    if len(msgs) > 0:
        raise ValueError("Problems with pre-aligned input BAM file: %s\n" % (in_bam)
                         + "\n".join(msgs) +
                         "\nSetting `bam_clean: picard` in the configuration can often fix this issue.")

def _check_bam_contigs(in_bam, ref_file, config):
    """Ensure a pre-aligned BAM file matches the expected reference genome.
    """
    ref_contigs = [c.name for c in ref.file_contigs(ref_file, config)]
    with contextlib.closing(pysam.Samfile(in_bam, "rb")) as bamfile:
        bam_contigs = [c["SN"] for c in bamfile.header["SQ"]]
    problems = []
    warnings = []
    for bc, rc in itertools.izip_longest(bam_contigs, ref_contigs):
        if bc != rc:
            if bc and rc:
                problems.append("Reference mismatch. BAM: %s Reference: %s" % (bc, rc))
            elif bc:
                problems.append("Extra BAM chromosomes: %s" % bc)
            elif rc:
                warnings.append("Extra reference chromosomes: %s" % rc)
    if problems:
        raise ValueError("Unexpected order, name or contig mismatches between input BAM and reference file:\n%s\n"
                         "Setting `bam_clean: picard` in the configuration can often fix this issue."
                         % "\n".join(problems))
    if warnings:
        print("*** Potential problems in input BAM compared to reference:\n%s\n" %
              "\n".join(warnings))


def open_samfile(in_file):
    if is_bam(in_file):
        return pysam.Samfile(in_file, "rb")
    elif is_sam(in_file):
        return pysam.Samfile(in_file, "r")
    else:
        raise IOError("in_file must be either a BAM file or SAM file. Is the "
                      "extension .sam or .bam?")

def is_bam(in_file):
    _, ext = os.path.splitext(in_file)
    if ext == ".bam":
        return True
    else:
        return False


def is_sam(in_file):
    _, ext = os.path.splitext(in_file)
    if ext == ".sam":
        return True
    else:
        return False

def mapped(in_bam, config):
    """
    return a bam file of only the mapped reads
    """
    out_file = os.path.splitext(in_bam)[0] + ".mapped.bam"
    if utils.file_exists(out_file):
        return out_file
    sambamba = _get_sambamba(config)
    with file_transaction(config, out_file) as tx_out_file:
        if sambamba:
            cmd = ("{sambamba} view --format=bam -F 'not (unmapped or mate_is_unmapped)' "
                   "{in_bam} -o {tx_out_file}")
        else:
            samtools = config_utils.get_program("samtools", config)
            cmd = "{samtools} view -b -F 4 {in_bam} -o {tx_out_file}"
        do.run(cmd.format(**locals()),
               "Filtering mapped reads to %s." % (tx_out_file))
    return out_file


def count(in_bam, config=None):
    """
    return the counts in a BAM file
    """
    if not config:
        config = {}
    sambamba = _get_sambamba(config)
    if sambamba:
        cmd = ("{sambamba} view -c {in_bam}").format(**locals())
    else:
        samtools = config_utils.get_program("samtools", config)
        cmd = ("{samtools} view -c {in_bam}").format(**locals())
    out = subprocess.check_output(cmd, shell=True)
    return int(out)


def sam_to_bam(in_sam, config):
    if is_bam(in_sam):
        return in_sam

    assert is_sam(in_sam), "%s is not a SAM file" % in_sam
    out_file = os.path.splitext(in_sam)[0] + ".bam"
    if utils.file_exists(out_file):
        return out_file

    samtools = config_utils.get_program("samtools", config)
    num_cores = config["algorithm"].get("num_cores", 1)
    with file_transaction(config, out_file) as tx_out_file:
        cmd = "{samtools} view -@ {num_cores} -h -S -b {in_sam} -o {tx_out_file}"
        do.run(cmd.format(**locals()),
               ("Convert SAM to BAM (%s cores): %s to %s"
                % (str(num_cores), in_sam, out_file)))
    return out_file

def sam_to_bam_stream_cmd(config, named_pipe=None):
    sambamba = config_utils.get_program("sambamba", config)
    num_cores = config["algorithm"].get("num_cores", 1)
    pipe = named_pipe if named_pipe else "/dev/stdin"
    cmd = " {sambamba} view --format=bam -S -t {num_cores} {pipe} ".format(**locals())
    return cmd


def bam_to_sam(in_file, config):
    if is_sam(in_file):
        return in_file

    assert is_bam(in_file), "%s is not a BAM file" % in_file
    out_file = os.path.splitext(in_file)[0] + ".sam"
    if utils.file_exists(out_file):
        return out_file

    samtools = config_utils.get_program("samtools", config)
    num_cores = config["algorithm"].get("num_cores", 1)
    with file_transaction(config, out_file) as tx_out_file:
        cmd = "{samtools} view -@ {num_cores} -h {in_file} -o {tx_out_file}"
        do.run(cmd.format(**locals()),
               ("Convert BAM to SAM (%s cores): %s to %s"
                % (str(num_cores), in_file, out_file)))
    return out_file


def reheader(header, bam_file, config):
    samtools = config_utils.get_program("samtools", config)
    base, ext = os.path.splitext(bam_file)
    out_file = base + ".reheadered" + ext
    cmd = "{samtools} reheader {header} {bam_file} > {out_file}"
    do.run(cmd.format(**locals()), "Reheadering %s." % bam_file)
    return out_file


def merge(bamfiles, out_bam, config):
    assert all(map(is_bam, bamfiles)), ("Not all of the files to merge are not BAM "
                                        "files: %s " % (bamfiles))
    assert all(map(utils.file_exists, bamfiles)), ("Not all of the files to merge "
                                                   "exist: %s" % (bamfiles))
    if len(bamfiles) == 1:
        return bamfiles[0]
    if os.path.exists(out_bam):
        return out_bam
    sambamba = _get_sambamba(config)
    sambamba = None
    samtools = config_utils.get_program("samtools", config)
    bamtools = config_utils.get_program("bamtools", config)
    num_cores = config["algorithm"].get("num_cores", 1)
    with file_transaction(config, out_bam) as tx_out_bam:
        try:
            if sambamba:
                cmd = "{sambamba} merge -t {num_cores} {tx_out_bam} " + " ".join(bamfiles)
            else:
                cmd = "{samtools} merge -@ {num_cores} {tx_out_bam} " + " ".join(bamfiles)
            do.run(cmd.format(**locals()), "Merge %s into %s." % (bamfiles, out_bam))
        except subprocess.CalledProcessError:
            files = " -in ".join(bamfiles)
            cmd = "{bamtools} merge -in {files} -out {tx_out_bam}"
            do.run(cmd.format(**locals()), "Error with other tools. Merge %s into %s with bamtools" % (bamfiles, out_bam))
    index(out_bam, config)
    return out_bam


def sort(in_bam, config, order="coordinate"):
    """Sort a BAM file, skipping if already present.
    """
    assert is_bam(in_bam), "%s in not a BAM file" % in_bam
    if bam_already_sorted(in_bam, config, order):
        return in_bam

    sort_stem = _get_sort_stem(in_bam, order)
    sort_file = sort_stem + ".bam"
    if not utils.file_exists(sort_file):
        sambamba = _get_sambamba(config)
        sambamba = None
        samtools = config_utils.get_program("samtools", config)
        num_cores = config["algorithm"].get("num_cores", 1)
        with file_transaction(config, sort_file) as tx_sort_file:
            tx_sort_stem = os.path.splitext(tx_sort_file)[0]
            tx_dir = utils.safe_makedir(os.path.dirname(tx_sort_file))
            order_flag = "-n" if order is "queryname" else ""
            samtools_cmd = ("{samtools} sort {order_flag} "
                            "{in_bam} {tx_sort_stem}")
            if sambamba:
                cmd = ("{sambamba} sort -t {num_cores} {order_flag} "
                       "-o {tx_sort_file} --tmpdir={tx_dir} {in_bam}")
            else:
                cmd = samtools_cmd
            # sambamba has intermittent multicore failures. Allow
            # retries with single core
            try:
                do.run(cmd.format(**locals()),
                       "Sort BAM file (multi core, %s): %s to %s" %
                       (order, os.path.basename(in_bam),
                        os.path.basename(sort_file)), log_error=False)
            except:
                do.run(samtools_cmd.format(**locals()),
                       "Sort BAM file (single core, %s): %s" %
                       (order, os.path.basename(in_bam),
                        os.path.basename(sort_file)))
    return sort_file

def sort_cmd(config, tmp_dir, named_pipe=None, order="coordinate"):
    """ Get a sort command, suitable for piping
    """
    sambamba = _get_sambamba(config)
    pipe = named_pipe if named_pipe else "/dev/stdin"
    order_flag = "-n" if order is "queryname" else ""
    num_cores = config["algorithm"].get("num_cores", 1)
    cmd = "{sambamba} sort --tmpdir {tmp_dir} -t {num_cores} {order_flag} {pipe}"
    return cmd.format(**locals())


def _get_sambamba(config):
    try:
        sambamba = config_utils.get_program("sambamba", config)
    except config_utils.CmdNotFound:
        sambamba = None
    return sambamba


def bam_already_sorted(in_bam, config, order):
    return order == _get_sort_order(in_bam, config)


def _get_sort_order(in_bam, config):
    with open_samfile(in_bam) as bam_handle:
        header = bam_handle.header
    return utils.get_in(header, ("HD", "SO"), None)

def _get_sort_stem(in_bam, order):
    SUFFIXES = {"coordinate": ".sorted", "queryname": ".nsorted"}
    sort_base = os.path.splitext(in_bam)[0]
    for suffix in SUFFIXES:
        sort_base = sort_base.split(suffix)[0]
    return sort_base + SUFFIXES[order]

def sample_name(in_bam):
    """
    get sample name from a BAM file as a work around for pysam 0.6 header
    parsing issues. This can get replaced by the pysam version in cortex.py
    when we move to Docker.
    """
    cmd = "samtools view -H {in_bam}".format(**locals())
    out, _ = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE).communicate()
    name = None
    for line in out.split("\n"):
        if line.startswith("@RG"):
            name = [x.split(":")[1] for x in line.split() if x.split(":")[0] == "SM"]
    return name[0] if name else None

def estimate_read_length(bam_file, nreads=1000):
    """
    estimate median read length of a SAM/BAM file
    """
    with open_samfile(bam_file) as bam_handle:
        reads = tz.itertoolz.take(nreads, bam_handle)
        lengths = [len(x.seq) for x in reads]
    return int(numpy.median(lengths))
