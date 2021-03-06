#!/usr/bin/env python2
from __future__ import print_function

# Requires PyVCF. To install: pip2 install pyvcf
import vcf
import argparse
import csv
from collections import defaultdict, namedtuple
import random
import sys
import numpy as np
import numpy.ma as ma
from scipy.stats.mstats import gmean

class ReadCountsUnavailableError(Exception):
  pass

class VariantParser(object):
  def __init__(self):
    # Child classes must give the following variables sensible values in
    # constructor so that list_variants() works subsequently.
    self._cnvs = None
    self._vcf_filename = None

  def list_variants(self):
    variants = self._filter(self._vcf_filename)
    variants_and_reads = []
    for variant in variants:
      try:
        ref_reads, total_reads = self._calc_read_counts(variant)
      except ReadCountsUnavailableError as exc:
        continue
      variants_and_reads.append((variant, ref_reads, total_reads))
    return variants_and_reads

  def _calc_read_counts(self, variant):
    raise Exception('Not implemented -- use child class')

  def _parse_vcf(self, vcf_filename):
    vcfr = vcf.Reader(filename=vcf_filename)
    records = []
    for variant in vcfr:
      variant.CHROM = variant.CHROM.lower()
      # Some VCF dialects prepend "chr", some don't. Remove the prefix to
      # standardize.
      if variant.CHROM.startswith('chr'):
        variant.CHROM = variant.CHROM[3:]
      records.append(variant)
    return records

  def _is_good_chrom(self, chrom):
    # Ignore the following:
    #   * Variants unmapped ('chrUn') or mapped to fragmented chromosome ('_random')
    #   * Weird chromosomes from Mutect (e.g., "chr17_ctg5_hap1").
    #   * Mitochondrial ("mt" or "m"), which are weird
    #   * Sex chromosomes difficult to deal with, as expected frequency depends on
    #     whether patient is male or female, so ignore them for now. TODO: fix this.
    if chrom in [str(i) for i in range(1, 23)]:
      return True
    else:
      return False

  def _does_variant_pass_filters(self, variant):
    if variant.FILTER is None:
      return True
    if len(variant.FILTER) > 0:
      # Variant failed one or more filters.
      return False
    return True

  def _filter(self, vcf_filename):
    variants = []

    all_variants = self._parse_vcf(vcf_filename)

    for variant in all_variants:
      if not self._is_good_chrom(variant.CHROM):
        continue
      if not self._does_variant_pass_filters(variant):
        continue
      variants.append(variant)
    return variants

  def _get_tumor_index(self, variant, tumor_sample=None):
    """Find the index of the tumor sample.

    Currently hardcodes tumour sample as the last column if name not specified.
    Might not always be true
    """
    if self._tumor_sample:
      tumor_is = [i for i, s in enumerate(variant.samples) if s.sample == tumor_sample]
      assert len(tumor_is) == 1, "Did not find tumor name %s in samples" % tumor_sample
      return tumor_is[0]
    else:
      # Don't make this -1, as some code assumes it will be >= 0.
      return len(variant.samples) - 1

class SangerParser(VariantParser):
  '''
  Works with PCAWG variant calls from the Sanger.
  '''
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _find_ref_and_variant_nt(self, variant):
    assert len(variant.REF) == len(variant.ALT) == 1
    return (str(variant.REF[0]), str(variant.ALT[0]))

  def _calc_read_counts(self, variant):
    normal = variant.genotype('NORMAL')
    tumor = variant.genotype('TUMOUR')

    reference_nt, variant_nt = self._find_ref_and_variant_nt(variant)
    tumor_reads = {
      'forward': {
        'A': int(tumor['FAZ']),
        'C': int(tumor['FCZ']),
        'G': int(tumor['FGZ']),
        'T': int(tumor['FTZ']),
      },
      'reverse': {
        'A': int(tumor['RAZ']),
        'C': int(tumor['RCZ']),
        'G': int(tumor['RGZ']),
        'T': int(tumor['RTZ']),
      },
    }

    ref_reads = tumor_reads['forward'][reference_nt] + tumor_reads['reverse'][reference_nt]
    # For now, variant reads are defined as only the non-reference nucleotide in
    # the inferred tumor SNP. We ignore reads of a third or fourth base.
    variant_reads = tumor_reads['forward'][variant_nt] + tumor_reads['reverse'][variant_nt]
    total_reads = ref_reads + variant_reads

    return (ref_reads, total_reads)

class PcawgConsensusParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _find_ref_and_variant_nt(self, variant):
    assert len(variant.REF) == len(variant.ALT) == 1
    return (str(variant.REF[0]), str(variant.ALT[0]))

  def _calc_read_counts(self, variant):
    if not ('t_alt_count' in variant.INFO and 't_ref_count' in variant.INFO):
      raise ReadCountsUnavailableError()
    assert len(variant.INFO['t_alt_count']) == len(variant.INFO['t_ref_count']) == 1

    alt_reads = int(variant.INFO['t_alt_count'][0])
    ref_reads = int(variant.INFO['t_ref_count'][0])
    total_reads = alt_reads + ref_reads
    # Some variants havezero alt and ref reads.
    if total_reads == 0:
      raise ReadCountsUnavailableError()
    return (ref_reads, total_reads)

class MuseParser(VariantParser):
  def __init__(self, vcf_filename, tier=0, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tier = tier
    self._tumor_sample = tumor_sample

  def _get_normal_genotype(self, variant):
    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    assert tumor_i in (0, 1), 'Tumor index %s is not 0 or 1' % tumor_i
    normal_i = 1 - tumor_i
    return set([int(t) for t in variant.samples[normal_i]['GT'].split('/')])

  def _calc_read_counts(self, variant):
    normal_gt = self._get_normal_genotype(variant)
    assert len(normal_gt) == 1
    normal_gt = normal_gt.pop()

    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    total_reads = int(variant.samples[tumor_i]['DP'])
    ref_reads = int(variant.samples[tumor_i]['AD'][normal_gt])

    return (ref_reads, total_reads)

  def _does_variant_pass_filters(self, variant):
    # Ignore heterozygous normal variants.
    if len(self._get_normal_genotype(variant)) != 1:
      return False
    if variant.FILTER is None or len(variant.FILTER) == 0:
      return True
    if int(variant.FILTER[0][-1]) <= self._tier:
      # Variant failed one or more filters, but we still accept it.
      return True
    return False
    
class StrelkaParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample    

  def _does_variant_pass_filters(self, variant):
    # Strelka outputs two files one for SNPs, the other for InDels
    # For now only deal with SNP file from Strelka
    if variant.is_snp:
      if variant.FILTER is None or len(variant.FILTER) == 0: 
        return True
    return False

  def _calc_read_counts(self, variant):
    alt = variant.ALT[0]
    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    total_reads = int(variant.samples[tumor_i]['DP'])

    if alt is None:
      total_reads = 0
      variant_reads = 0
    else:
      variant_reads = int(getattr(variant.samples[tumor_i].data, str(alt)+'U')[0])

    ref_reads = total_reads - variant_reads
    return (ref_reads, total_reads)

class MutectTcgaParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _calc_read_counts(self, variant):
    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    # TD: Tumor allelic depths for the ref and alt alleles in the order listed
    ref_reads, variant_reads = variant.samples[tumor_i]['TD']
    total_reads = ref_reads + variant_reads
    return (ref_reads, total_reads)

class MutectPcawgParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _calc_read_counts(self, variant):
    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    ref_reads = int(variant.samples[tumor_i].data.ref_count)
    variant_reads = int(variant.samples[tumor_i].data.alt_count)
    total_reads = ref_reads + variant_reads

    return (ref_reads, total_reads)

class MutectSmchetParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _calc_read_counts(self, variant):
    tumor_i = self._get_tumor_index(variant, self._tumor_sample)
    ref_reads = int(variant.samples[tumor_i]['AD'][0])
    variant_reads = int(variant.samples[tumor_i]['AD'][1])
    total_reads = ref_reads + variant_reads

    return (ref_reads, total_reads)

class VarDictParser(MutectSmchetParser):
  """Support VarDict somatic variant caller.

  https://github.com/AstraZeneca-NGS/VarDictJava
  https://github.com/AstraZeneca-NGS/VarDict

  Uses the same read-extraction logic as MuTect (SMC-Het).
  """
  pass

class DKFZParser(VariantParser):
  def __init__(self, vcf_filename, tumor_sample=None):
    self._vcf_filename = vcf_filename
    self._tumor_sample = tumor_sample

  def _calc_read_counts(self, variant):
    # This doesn't handle multisample correctly, as I don't know how to get the
    # DP4 attribute on multiple DKFZ samples currently.
    for_ref_reads = int(variant.INFO['DP4'][0])
    back_ref_reads = int(variant.INFO['DP4'][1])
    for_variant_reads = int(variant.INFO['DP4'][2])
    back_variant_reads = int(variant.INFO['DP4'][3])
    ref_reads = for_ref_reads + back_ref_reads
    var_reads = for_variant_reads + back_variant_reads
    total_reads = ref_reads + var_reads

    return (ref_reads, total_reads)

class CnvFormatter(object):
  def __init__(self, cnv_confidence, cellularity, read_depth, read_length):
    self._cnv_confidence = cnv_confidence
    self._cellularity = cellularity
    self._read_depth = read_depth
    self._read_length = read_length

  def _max_reads(self):
    return 1e6 * self._read_depth

  def _find_overlapping_variants(self, chrom, cnv, variants):
    overlapping = []

    start = cnv['start']
    end = cnv['end']
    for variant in variants:
      if chrom.lower() == variant['chrom'].lower():
        if start <= variant['pos'] <= end:
          overlapping.append(variant['ssm_id'])
    return overlapping

  def _calc_ref_reads(self, cellular_prev, total_reads):
    vaf = cellular_prev / 2
    ref_reads = int((1 - vaf) * total_reads)
    return ref_reads

  def _calc_total_reads(self, cellular_prev, locus_start, locus_end, new_cn):
    # Proportion of all cells carrying CNV.
    P = cellular_prev
    if new_cn == 2:
      # If no net change in copy number -- e.g., because (major, minor) went
      # from (1, 1) to (2, 0) -- force the delta_cn to be 1.
      delta_cn = 1.
      no_net_change = True
    else:
      delta_cn = float(new_cn - 2)
      no_net_change = False

    region_length = locus_end - locus_start + 1
    fn = (self._read_depth * region_length) / self._read_length

    # This is a hack to prevent division by zero (when delta_cn = -2). Its
    # effect will be to make d large.
    if P == 1.0:
      P = 0.999

    d = (delta_cn**2 / 4) * (fn * P * (2 - P)) / (1 + (delta_cn  * P) / 2)

    if no_net_change:
      # If no net change in CN occurred, the estimate was just based on BAFs,
      # meaning we have lower confidence in it. Indicate this lack of
      # confidence via d by multiplying it by (read length / distance between
      # common SNPs), with the "distance between common SNPs" taken to be 1000 bp.
      d *= (self._read_length / 1000.)

    # Cap at 1e6 * read_depth.
    return int(round(min(d, self._max_reads())))

  def _format_overlapping_variants(self, variants, maj_cn, min_cn):
      variants = [(ssm_id, str(min_cn), str(maj_cn)) for ssm_id in variants]
      return variants

  def _format_cnvs(self, cnvs, variants):
    log('Estimated read depth: %s' % self._read_depth)

    for chrom, chrom_cnvs in cnvs.items():
      for cnv in chrom_cnvs:
        overlapping_variants = self._find_overlapping_variants(chrom, cnv, variants)
        total_reads = self._calc_total_reads(
          cnv['cellular_prevalence'],
          cnv['start'],
          cnv['end'],
          cnv['major_cn'] + cnv['minor_cn'],
        )
        yield {
          'chrom': chrom,
          'start': cnv['start'],
          'end': cnv['end'],
          'major_cn': cnv['major_cn'],
          'minor_cn': cnv['minor_cn'],
          'cellular_prevalence': cnv['cellular_prevalence'],
          'ref_reads': self._calc_ref_reads(cnv['cellular_prevalence'], total_reads),
          'total_reads': total_reads,
          'overlapping_variants': self._format_overlapping_variants(overlapping_variants, cnv['major_cn'], cnv['minor_cn']),
        }

  def _merge_variants(self, cnv1, cnv2):
    cnv1_variant_names = set([v[0] for v in cnv1['overlapping_variants']])
    for variant in cnv2['overlapping_variants']:
      variant_name = variant[0]
      if variant_name not in cnv1_variant_names:
        cnv1['overlapping_variants'].append(variant)
      else:
        # If variant already in cnv1's list, ignore it. This should only occur
        # if two subclonal CNVs have close to 0.5 frequency each. In this case,
        # we lose information about major/minor status of the cnv2 relative to
        # its SSMs.
        log('%s already in %s' % (variant, cnv1['cnv_id']))

  # CNVs with similar a/d values should not be free to move around the
  # phylogeny independently, and so we merge them into a single entity. We may
  # do the same with SNVs bearing similar frequencies later on.
  def format_and_merge_cnvs(self, cnvs, variants):
    formatted = list(self._format_cnvs(cnvs, variants))
    formatted.sort(key = lambda f: f['cellular_prevalence'])
    if len(formatted) == 0:
      return []

    merged, formatted = formatted[:1], formatted[1:]
    merged[0]['cnv_id'] = 'c0'
    counter = 1

    cellularity = find_cellularity(cnvs)

    for current in formatted:
      last = merged[-1]

      # Only merge CNVs if they're clonal. If they're subclonal, leave them
      # free to move around the tree.
      if current['cellular_prevalence'] == last['cellular_prevalence'] == cellularity:
        # Merge the CNVs.
        log('Merging %s_%s and %s_%s' % (current['chrom'], current['start'], last['chrom'], last['start']))
        last['total_reads'] = current['total_reads'] + last['total_reads']
        last['ref_reads'] = self._calc_ref_reads(last['cellular_prevalence'], last['total_reads'])
        self._merge_variants(last, current)
      else:
        # Do not merge the CNVs.
        current['cnv_id'] = 'c%s' % counter
        merged.append(current)
        counter += 1

    for cnv in merged:
      cnv['ref_reads'] = int(round(cnv['ref_reads'] * self._cnv_confidence))
      cnv['total_reads'] = int(round(cnv['total_reads'] * self._cnv_confidence))

    return merged

class VariantFormatter(object):
  def __init__(self):
    self._counter = 0

  def _split_types(self, genotype):
    types = [int(e) for e in genotype.split('/')]
    if len(types) != 2:
      raise Exception('Not diploid: %s' % types)
    return types

  def _calc_ref_freq(self, ref_genotype, error_rate):
    types = self._split_types(ref_genotype)
    num_ref = len([t for t in types if t == 0])
    freq = (num_ref / 2) - error_rate
    if freq < 0:
      freq = 0.0
    if freq > 1:
      raise Exception('Nonsensical frequency: %s' % freq)
    return freq

  def format_variants(self, variants, ref_read_counts, total_read_counts, error_rate):
    for variant_idx, variant in enumerate(variants):
      ssm_id = 's%s' % self._counter
      if hasattr(variant, 'ID') and variant.ID is not None:
        # This field will be defined by PyVCF, but not by our VariantId named
        # tuple that we have switched to, so this code will never actually run.
        # TODO: fix that.
        variant_name = variant.ID
      else:
        variant_name = '%s_%s' % (variant.CHROM, variant.POS)

      # TODO: switch back to using calc_ref_freq() when we no longer want mu_r
      # and mu_v fixed.
      # This is mu_r in PhyloWGS.
      expected_ref_freq = 1 - error_rate
      if variant.CHROM in ('x', 'y', 'm'):
        # Haploid, so should only see non-variants when sequencing error
        # occurred. Note that chrY and chrM are always haploid; chrX is haploid
        # only in men, so script must know sex of patient to choose correct
        # value. Currently, I just assume that all data comes from men.
        #
        # This is mu_v in PhyloWGS.
        expected_var_freq = error_rate
      else:
        # Diploid, so should see variants in (0.5 - error_rate) proportion of
        # reads.
        #
        # This is mu_v in PhyloWGS.
        expected_var_freq = 0.5 - error_rate

      yield {
        'ssm_id': ssm_id,
        'chrom': variant.CHROM,
        'pos': variant.POS,
        'variant_name': variant_name,
        'ref_reads': list(ref_read_counts[variant_idx,:]),
        'total_reads': list(total_read_counts[variant_idx,:]),
        'expected_ref_freq': expected_ref_freq,
        'expected_var_freq': expected_var_freq,
      }
      self._counter += 1

def restricted_float(x):
  x = float(x)
  if x < 0.0 or x > 1.0:
    raise argparse.ArgumentTypeError('%r not in range [0.0, 1.0]' % x)
  return x

def variant_key(var):
  chrom = var.CHROM
  if chrom == 'x':
    chrom = 100
  elif chrom == 'y':
    chrom = 101
  else:
    chrom = int(chrom)
  return (chrom, var.POS)

def find_cellularity(cnvs):
  max_cellular_prev = 0
  for chrom, chrom_regions in cnvs.items():
    for cnr in chrom_regions:
      if cnr['cellular_prevalence'] > max_cellular_prev:
        max_cellular_prev = cnr['cellular_prevalence']
  return max_cellular_prev

class VariantAndCnvGroup(object):
  def __init__(self):
    self._cn_regions = None
    self._cellularity = None

  def add_variants(self, variants, ref_read_counts, total_read_counts):
    self._variants = variants
    self._variant_idxs = list(range(len(variants)))
    self._ref_read_counts = ref_read_counts
    self._total_read_counts = total_read_counts
    # Estimate read depth before any filtering of variants is performed, in
    # case no SSMs remain afterward.
    self._estimated_read_depth = self._estimate_read_depth()

  def add_cnvs(self, cn_regions):
    self._cn_regions = cn_regions
    self._cellularity = find_cellularity(self._cn_regions)

  def has_cnvs(self):
    return self._cn_regions is not None

  def _filter_variants_outside_regions(self, regions, before_label, after_label):
    filtered = []

    for vidx in self._variant_idxs:
      variant = self._variants[vidx]
      for region in regions[variant.CHROM]:
        if region['start'] <= variant.POS <= region['end']:
          filtered.append(vidx)
          break

    self._print_variant_differences(
      [self._variants[idx] for idx in self._variant_idxs],
      [self._variants[idx] for idx in filtered],
      before_label,
      after_label
    )
    self._variant_idxs = filtered

  def _is_region_normal_cn(self, region):
    return region['major_cn'] == region['minor_cn'] == 1

  def _print_variant_differences(self, before, after, before_label, after_label):
    before = set(before)
    after = set(after)
    log('%s=%s %s=%s delta=%s' % (before_label, len(before), after_label, len(after), len(before) - len(after)))

    assert after.issubset(before)
    removed = list(before - after)
    removed.sort(key = variant_key)

    for var in removed:
      var_name = '%s_%s' % (var.CHROM, var.POS)
      for region in self._cn_regions[var.CHROM]:
        if region['start'] <= var.POS <= region['end']:
          region_type = (self._is_region_normal_cn(region) and 'normal') or 'abnormal'
          log('%s\t[in %s-CN region chr%s(%s, %s)]' % (var_name, region_type, var.CHROM, region['start'], region['end']))
          break
      else:
        log('%s\t[outside all regions]' % var_name)

  def retain_only_variants_in_normal_cn_regions(self):
    if not self.has_cnvs():
      raise Exception('CN regions not yet provided')

    normal_cn = defaultdict(list)

    for chrom, regions in self._cn_regions.items():
      for region in regions:
        if self._is_region_normal_cn(region) and region['cellular_prevalence'] == self._cellularity:
          normal_cn[chrom].append(region)

    filtered = self._filter_variants_outside_regions(normal_cn, 'all_variants', 'only_normal_cn')

  def _filter_multiple_abnormal_cn_regions(self, regions):
    good_regions = defaultdict(list)
    for chrom, reg in regions.items():
      idx = 0
      while idx < len(reg):
        region = reg[idx]

        # Accept clonal regions unconditonally, whether normal or abnormal CN.
        if region['cellular_prevalence'] == self._cellularity:
          good_regions[chrom].append(region)
          idx += 1

        else:
          regions_at_same_coords = [region]

          i = idx + 1
          while i < len(reg) and reg[i]['start'] == region['start']:
            # Battenerg either has entire regions at same coords, or they have
            # no overlap whatsoever. Thus, this assertion maintains sanity for
            # Battenberg, but may fail for other CN callers.
            assert reg[i]['end'] == region['end']
            regions_at_same_coords.append(reg[i])
            i += 1

          abnormal_regions = [r for r in regions_at_same_coords if not self._is_region_normal_cn(r)]
          # In Battenberg, either one region is normal and the other abnormal,
          # or both are abnormal.
          # In TITAN, only one abnormal region will be listed, without a
          # corresponding normal region.
          # Ignore normal region(s) and add only one abnormal one. We do
          # this so PWGS can recalculate the frequencies based on the
          # major/minor CN of the region, according to the a & d values we will
          # assign to the region.
          if len(abnormal_regions) == 1:
            good_regions[chrom].append(abnormal_regions[0])
          else:
            # Ignore CNV regions with multiple abnormal CN states, as we don't
            # know what order the CN events occurred in.
            log('Multiple abnormal regions: chrom=%s %s' % (chrom, abnormal_regions))
          idx += len(regions_at_same_coords)

    return good_regions

  def exclude_variants_in_subclonal_cnvs(self):
    # Battenberg:
    #   Five possible placements for variant in Battenberg according to CN records:
    #   1 record:
    #     That record has normal CN: include
    #     That record has abnormal CN: include
    #   2 records:
    #     One record is normal CN, one record is abnormal CN: include
    #     Both records are abnormal CN: exclude (as we don't know what order the CN events occurred in)
    # TITAN:
    #   In output seen to date, TITAN will only list one record per region. If
    #   the CN state is abnormal and clonal_frac < 1, this implies the
    #   remainder of the region will be normal CN. Multiple abnormal records
    #   for the same region are likely possible, but I haven't yet seen any.
    #   Regardless, when they occur, they should be properly handled by the
    #   code.
    if not self.has_cnvs():
      raise Exception('CN regions not yet provided')

    good_regions = self._filter_multiple_abnormal_cn_regions(self._cn_regions)
    # If variant isn't listed in *any* region: exclude (as we suspect CNV
    # caller didn't know what to do with the region).
    self._filter_variants_outside_regions(good_regions, 'all_variants', 'outside_subclonal_cn')

  def format_variants(self, sample_size, error_rate, priority_ssms):
    if sample_size is None:
      sample_size = len(self._variant_idxs)
    random.shuffle(self._variant_idxs)

    subsampled, remaining = [], []

    for variant_idx in self._variant_idxs:
      variant = self._variants[variant_idx]
      if len(subsampled) < sample_size and (variant.CHROM, variant.POS) in priority_ssms:
        subsampled.append(variant_idx)
      else:
        remaining.append(variant_idx)

    assert len(subsampled) <= sample_size
    needed = sample_size - len(subsampled)
    subsampled = subsampled + remaining[:needed]
    nonsubsampled = remaining[needed:]

    subsampled.sort(key = lambda idx: variant_key(self._variants[idx]))
    subsampled_variants = get_elements_at_indices(self._variants, subsampled)
    subsampled_ref_counts = self._ref_read_counts[subsampled,:]
    subsampled_total_counts = self._total_read_counts[subsampled,:]

    nonsubsampled.sort(key = lambda idx: variant_key(self._variants[idx]))
    nonsubsampled_variants = get_elements_at_indices(self._variants, nonsubsampled)
    nonsubsampled_ref_counts = self._ref_read_counts[nonsubsampled,:]
    nonsubsampled_total_counts = self._total_read_counts[nonsubsampled,:]

    formatter = VariantFormatter()
    subsampled_formatted = list(formatter.format_variants(subsampled_variants, subsampled_ref_counts, subsampled_total_counts, error_rate))
    nonsubsampled_formatted = list(formatter.format_variants(nonsubsampled_variants, nonsubsampled_ref_counts, nonsubsampled_total_counts, error_rate))

    return (subsampled_formatted, nonsubsampled_formatted)

  def write_variants(self, variants, outfn):
    with open(outfn, 'w') as outf:
      print('\t'.join(('id', 'gene', 'a', 'd', 'mu_r', 'mu_v')), file=outf)
      for variant in variants:
        variant['ref_reads'] = ','.join([str(v) for v in variant['ref_reads']])
        variant['total_reads'] = ','.join([str(v) for v in variant['total_reads']])
        vals = (
          'ssm_id',
          'variant_name',
          'ref_reads',
          'total_reads',
          'expected_ref_freq',
          'expected_var_freq',
        )
        vals = [variant[k] for k in vals]
        print('\t'.join([str(v) for v in vals]), file=outf)

  def _estimate_read_depth(self):
    read_sum = 0
    if len(self._variants) == 0:
      default_read_depth = 50
      log('No variants available, so fixing read depth at %s.' % default_read_depth)
      return default_read_depth
    else:
      return np.nanmean(self._total_read_counts)

  def write_cnvs(self, variants, outfn, cnv_confidence, read_length):
    abnormal_regions = {}
    filtered_regions = self._filter_multiple_abnormal_cn_regions(self._cn_regions)
    for chrom, regions in filtered_regions.items():
      abnormal_regions[chrom] = [r for r in regions if not self._is_region_normal_cn(r)]

    with open(outfn, 'w') as outf:
      print('\t'.join(('cnv', 'a', 'd', 'ssms')), file=outf)
      formatter = CnvFormatter(cnv_confidence, self._cellularity, self._estimated_read_depth, read_length)
      for cnv in formatter.format_and_merge_cnvs(abnormal_regions, variants):
        overlapping = [','.join(o) for o in cnv['overlapping_variants']]
        vals = (
          cnv['cnv_id'],
          str(cnv['ref_reads']),
          str(cnv['total_reads']),
          ';'.join(overlapping),
        )
        print('\t'.join(vals), file=outf)

def log(msg):
  if log.verbose:
    print(msg, file=sys.stderr)
log.verbose = False

class CnvParser(object):
  def __init__(self, cn_filename):
    self._cn_filename = cn_filename

  def parse(self):
    cn_regions = defaultdict(list)

    with open(self._cn_filename) as cnf:
      reader = csv.DictReader(cnf, delimiter='\t')
      for record in reader:
        chrom = record['chromosome']
        del record['chromosome']
        for key in ('start', 'end', 'major_cn', 'minor_cn'):
          record[key] = int(record[key])
        record['cellular_prevalence'] = float(record['cellular_prevalence'])
        cn_regions[chrom].append(record)

    # Ensure CN regions are properly sorted, which we later rely on when
    # filtering out regions with multiple abnormal CN states.
    for chrom, regions in cn_regions.items():
      cn_regions[chrom] = sorted(regions, key = lambda r: r['start'])

    return cn_regions

def get_elements_at_indices(L, indices):
  elem = []
  for idx in indices:
    elem.append(L[idx])
  return elem

def parse_priority_ssms(priority_ssm_filename):
  if priority_ssm_filename is None:
    return set()
  with open(priority_ssm_filename) as priof:
    lines = priof.readlines()

  priority_ssms = set()
  for line in lines:
    chrom, pos = line.strip().split('_', 1)
    priority_ssms.add((chrom.lower(), int(pos)))
  return set(priority_ssms)

def impute_missing_total_reads(total_reads, missing_variant_confidence):
  # Change NaNs to masked values via SciPy.
  masked_total_reads = ma.fix_invalid(total_reads)

  # Going forward, suppose you have v variants and s samples in a v*s matrix of
  # read counts. Missing values are masked.

  # Calculate geometric mean of variant read depth in each sample. Result: s*1
  sample_means = gmean(masked_total_reads, axis=0)
  assert np.sum(sample_means <= 0) == np.sum(np.isnan(sample_means)) == 0
  # Divide every variant's read count by its mean sample read depth to get read
  # depth enrichment relative to other variants in sample. Result: v*s
  normalized_to_sample = np.dot(masked_total_reads, np.diag(1./sample_means))
  # For each variant, calculate geometric mean of its read depth enrichment
  # across samples. Result: v*1
  variant_mean_reads = gmean(normalized_to_sample, axis=1)
  assert np.sum(variant_mean_reads <= 0) == np.sum(np.isnan(variant_mean_reads)) == 0

  # Convert 1D arrays to vectors to permit matrix multiplication.
  imputed_counts = np.dot(variant_mean_reads.reshape((-1, 1)), sample_means.reshape((1, -1)))
  nan_coords = np.where(np.isnan(total_reads))
  total_reads[nan_coords] = imputed_counts[nan_coords]
  assert np.sum(total_reads <= 0) == np.sum(np.isnan(total_reads)) == 0

  total_reads[nan_coords] *= missing_variant_confidence
  return np.floor(total_reads).astype(np.int)

def impute_missing_ref_reads(ref_reads, total_reads):
  ref_reads = np.copy(ref_reads)

  assert np.sum(np.isnan(total_reads)) == 0
  nan_coords = np.where(np.isnan(ref_reads))
  ref_reads[nan_coords] = total_reads[nan_coords]
  assert np.sum(np.isnan(ref_reads)) == 0

  return ref_reads.astype(np.int)

def parse_variants(args, vcf_types):
  num_samples = len(args.vcf_files)
  parsed_variants = []
  all_variant_ids = []

  VariantId = namedtuple('VariantId', ['CHROM', 'POS'])

  for vcf_file in args.vcf_files:
    vcf_type, vcf_fn = vcf_file.split('=', 1)

    if vcf_type == 'sanger':
      variant_parser = SangerParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'mutect_pcawg':
      variant_parser = MutectPcawgParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'mutect_smchet':
      variant_parser = MutectSmchetParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'mutect_tcga':
      variant_parser = MutectTcgaParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'muse':
      variant_parser = MuseParser(vcf_fn, args.muse_tier, args.tumor_sample)
    elif vcf_type == 'dkfz':
      variant_parser = DKFZParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'strelka':
      variant_parser = StrelkaParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'vardict':
      variant_parser = VarDictParser(vcf_fn, args.tumor_sample)
    elif vcf_type == 'pcawg_consensus':
      variant_parser = PcawgConsensusParser(vcf_fn, args.tumor_sample)
    else:
      raise Exception('Unknowon variant type: %s' % vcf_type)

    parsed_variants.append(variant_parser.list_variants())
    variant_ids = [VariantId(str(v[0].CHROM), int(v[0].POS)) for v in parsed_variants[-1]]
    all_variant_ids += variant_ids

  all_variant_ids = list(set(all_variant_ids)) # Eliminate duplicates.
  all_variant_ids.sort(key = variant_key)
  num_variants = len(all_variant_ids)
  variant_positions = dict(zip(all_variant_ids, range(num_variants)))

  total_read_counts = np.zeros((num_variants, num_samples))
  total_read_counts.fill(np.nan)
  ref_read_counts = np.copy(total_read_counts)

  for sample_idx, parsed in enumerate(parsed_variants):
    for variant, ref_reads, total_reads in parsed:
      variant_id = VariantId(str(variant.CHROM), int(variant.POS))
      variant_idx = variant_positions[variant_id]
      ref_read_counts[variant_idx, sample_idx] = ref_reads
      total_read_counts[variant_idx, sample_idx] = total_reads

  total_read_counts = impute_missing_total_reads(total_read_counts, args.missing_variant_confidence)
  ref_read_counts = impute_missing_ref_reads(ref_read_counts, total_read_counts)
  return (all_variant_ids, ref_read_counts, total_read_counts)

def main():
  vcf_types = set(('sanger', 'mutect_pcawg', 'mutect_smchet', 'mutect_tcga', 'muse','dkfz', 'strelka', 'vardict', 'pcawg_consensus'))

  parser = argparse.ArgumentParser(
    description='Create ssm_dat.txt and cnv_data.txt input files for PhyloWGS from VCF and CNV data.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
  )
  parser.add_argument('-e', '--error-rate', dest='error_rate', type=restricted_float, default=0.001,
    help='Expected error rate of sequencing platform')
  parser.add_argument('--missing-variant-confidence', dest='missing_variant_confidence', type=restricted_float, default=1.,
    help='Confidence in range [0, 1] that SSMs missing from a sample are indeed not present in that sample')
  parser.add_argument('-s', '--sample-size', dest='sample_size', type=int,
    help='Subsample SSMs to reduce PhyloWGS runtime')
  parser.add_argument('-P', '--priority-ssms', dest='priority_ssm_filename',
    help='File containing newline-separated list of SSMs in "<chr>_<locus>" format to prioritize for inclusion')
  parser.add_argument('--cnvs', dest='cnv_file',
    help='Path to CNV list created with parse_cnvs.py')
  parser.add_argument('--only-normal-cn', dest='only_normal_cn', action='store_true', default=False,
      help='Only output variants lying in normal CN regions. Do not output CNV data directly.')
  parser.add_argument('--output-cnvs', dest='output_cnvs', default='cnv_data.txt',
    help='Output destination for CNVs')
  parser.add_argument('--output-variants', dest='output_variants', default='ssm_data.txt',
    help='Output destination for variants')
  parser.add_argument('--tumor-sample', dest='tumor_sample',
    help='Name of the tumor sample in the input VCF file. Defaults to last sample if not specified.')
  parser.add_argument('--cnv-confidence', dest='cnv_confidence', type=restricted_float, default=0.5,
    help='Confidence in CNVs. Set to < 1 to scale "d" values used in CNV output file')
  parser.add_argument('--read-length', dest='read_length', type=int, default=100,
    help='Approximate length of reads. Used to calculate confidence in CNV frequencies')
  parser.add_argument('--muse-tier', dest='muse_tier', type=int, default=0,
    help='Maximum MuSE tier to include')
  parser.add_argument('--nonsubsampled-variants', dest='output_nonsubsampled_variants',
    help='If subsampling, write nonsubsampled variants to separate file, in addition to subsampled variants')
  parser.add_argument('--nonsubsampled-variants-cnvs', dest='output_nonsubsampled_variants_cnvs',
    help='If subsampling, write CNVs for nonsubsampled variants to separate file')
  parser.add_argument('--verbose', dest='verbose', action='store_true')
  parser.add_argument('vcf_files', nargs='+', help='One or more space-separated occurrences of <vcf_type>=<path>. E.g., sanger=variants1.vcf muse=variants2.vcf. Valid vcf_type values: %s' % ', '.join(vcf_types))
  args = parser.parse_args()

  log.verbose = args.verbose

  variant_ids, ref_read_counts, total_read_counts = parse_variants(args, vcf_types)

  # Fix random seed to ensure same set of SSMs chosen when subsampling on each
  # invocation.
  random.seed(1)

  grouper = VariantAndCnvGroup()
  grouper.add_variants(variant_ids, ref_read_counts, total_read_counts)

  if args.cnv_file:
    cnv_parser = CnvParser(args.cnv_file)
    cn_regions = cnv_parser.parse()
    grouper.add_cnvs(cn_regions)

  if args.only_normal_cn:
    grouper.retain_only_variants_in_normal_cn_regions()
  elif grouper.has_cnvs():
    grouper.exclude_variants_in_subclonal_cnvs()

  priority_ssms = parse_priority_ssms(args.priority_ssm_filename)
  subsampled_vars, nonsubsampled_vars = grouper.format_variants(args.sample_size, args.error_rate, priority_ssms)
  grouper.write_variants(subsampled_vars, args.output_variants)
  if args.output_nonsubsampled_variants:
    grouper.write_variants(nonsubsampled_vars, args.output_nonsubsampled_variants)

  if not args.only_normal_cn and grouper.has_cnvs():
    grouper.write_cnvs(subsampled_vars, args.output_cnvs, args.cnv_confidence, args.read_length)
    if args.output_nonsubsampled_variants and args.output_nonsubsampled_variants_cnvs:
      grouper.write_cnvs(nonsubsampled_vars, args.output_nonsubsampled_variants_cnvs, args.cnv_confidence, args.read_length)
  else:
    # Write empty CNV file.
    with open(args.output_cnvs, 'w'):
      pass

if __name__ == '__main__':
  main()
