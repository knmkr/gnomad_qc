from gnomad_hail import *
from collections import Counter

DOWNSAMPLINGS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000, 50000,
                 55000, 60000, 65000, 70000, 75000, 80000, 85000, 90000, 95000, 100000, 110000, 120000]
POPS_TO_REMOVE_FOR_POPMAX = ['asj', 'fin', 'oth']


def generate_downsamplings_cumulative(mt: hl.MatrixTable) -> Tuple[hl.MatrixTable, List[int]]:
    pop_data = [x[0] for x in get_sample_data(mt, [mt.meta.pop])]
    pops = Counter(pop_data)
    downsamplings = DOWNSAMPLINGS + list(pops.values())
    downsamplings = sorted([x for x in downsamplings if x <= sum(pops.values())])
    kt = mt.cols()
    kt = kt.annotate(r=hl.rand_unif(0, 1))
    kt = kt.order_by(kt.r).add_index('global_idx')

    for i, pop in enumerate(pops):
        pop_kt = kt.filter(kt.meta.pop == pop).add_index('pop_idx')
        if not i:
            global_kt = pop_kt
        else:
            global_kt = global_kt.union(pop_kt)
    global_kt = global_kt.key_by('s')
    return mt.annotate_cols(downsampling=global_kt[mt.s]), downsamplings


def add_faf_expr(freq: hl.expr.ArrayExpression, freq_meta: hl.expr.ArrayExpression, locus: hl.expr.LocusExpression, populations: Set[str]) -> hl.expr.ArrayExpression:
    """
    Calculates popmax (add an additional entry into freq with popmax: pop)

    :param ArrayExpression freq: ArrayExpression of Structs with ['ac', 'an', 'hom']
    :param ArrayExpression freq_meta: ArrayExpression of meta dictionaries corresponding to freq
    :param LocusExpression locus: LocusExpression
    :param set of str populations: Set of populations over which to calculate popmax
    :return: Frequency data with annotated popmax
    :rtype: ArrayExpression
    """
    pops_to_use = hl.literal(populations)
    freq = hl.map(lambda x: x[0].annotate(meta=x[1]), hl.zip(freq, freq_meta))
    freqs_to_use = hl.filter(lambda f:
                             ((f.meta.size() == 1) & (f.meta.get('group') == 'adj')) |
                             ((f.meta.size() == 2) & (f.meta.get('group') == 'adj') & pops_to_use.contains(f.meta.get('pop'))) |
                             (~locus.in_autosome_or_par() & (
                                     ((f.meta.size() == 2) & (f.meta.get('group') == 'adj') & f.meta.contains('sex')) |
                                     ((f.meta.size() == 3) & (f.meta.get('group') == 'adj') & pops_to_use.contains(f.meta.get('pop')) & f.meta.contains('sex')))),
                             freq)
    return freqs_to_use.map(lambda f: hl.struct(
        meta=f.meta,
        faf95=hl.experimental.filtering_allele_frequency(f.AC[1], f.AN, 0.95),
        faf99=hl.experimental.filtering_allele_frequency(f.AC[1], f.AN, 0.99)
    ))


def generate_frequency_data(mt: hl.MatrixTable, calculate_downsampling: bool = False,
                            calculate_by_platform: bool = False) -> Tuple[hl.Table, hl.Table]:
    """
    :param MatrixTable mt: Input MatrixTable
    :param bool calculate_downsampling: Calculate frequencies for downsampled data
    :param bool calculate_by_platform: Calculate frequencies for PCR-free data
    """
    if calculate_downsampling:
        mt, downsamplings = generate_downsamplings_cumulative(mt)
        print(f'Got {len(downsamplings)} downsamplings: {downsamplings}')
    cut_dict = {'pop': hl.agg.counter(hl.agg.filter(hl.is_defined(mt.meta.pop), mt.meta.pop)),
                'sex': hl.agg.collect_as_set(hl.agg.filter(hl.is_defined(mt.meta.sex), mt.meta.sex)),
                'subpop': hl.agg.collect_as_set(
        hl.agg.filter(hl.is_defined(mt.meta.subpop) & hl.is_defined(mt.meta.pop),
                      hl.struct(subpop=mt.meta.subpop, pop=mt.meta.pop)))
    }
    if calculate_by_platform:
        cut_dict['platform'] = hl.agg.collect_as_set(
            hl.agg.filter(hl.is_defined(mt.meta.qc_platform), mt.meta.qc_platform)
        )
    cut_data = mt.aggregate_cols(hl.struct(**cut_dict))

    sample_group_filters = [({}, True)]
    sample_group_filters.extend([
        ({'pop': pop}, mt.meta.pop == pop) for pop in cut_data.pop
    ] + [
        ({'sex': sex}, mt.meta.sex == sex) for sex in cut_data.sex
    ] + [
        ({'pop': pop, 'sex': sex}, (mt.meta.sex == sex) & (mt.meta.pop == pop))
        for sex in cut_data.sex for pop in cut_data.pop
    ] + [
        ({'subpop': subpop.subpop, 'pop': subpop.pop},
         mt.meta.subpop == subpop.subpop)
        for subpop in cut_data.subpop
    ])

    if calculate_by_platform:
        sample_group_filters.extend([
            ({'platform': str(platform)}, mt.meta.qc_platform == platform)
            for platform in cut_data.platform
        ])

    if calculate_downsampling:
        sample_group_filters.extend([
            ({'downsampling': str(ds), 'pop': 'global'},
             mt.downsampling.global_idx < ds) for ds in downsamplings
        ])
        sample_group_filters.extend([
            ({'downsampling': str(ds), 'pop': pop},
             (mt.downsampling.pop_idx < ds) & (mt.meta.pop == pop))
            for ds in downsamplings for pop, pop_count in cut_data.pop.items() if ds <= pop_count
        ])
    mt = mt.select_cols(group_membership=tuple(x[1] for x in sample_group_filters), project_id=mt.meta.project_id, age=mt.meta.age)
    mt = mt.select_rows()

    frequency_expression = []
    meta_expressions = []
    for i in range(len(sample_group_filters)):
        subgroup_dict = sample_group_filters[i][0]
        subgroup_dict['group'] = 'adj'
        frequency_expression.append(hl.agg.call_stats(hl.agg.filter(mt.group_membership[i] & mt.adj, mt.GT), mt.alleles))
        meta_expressions.append(subgroup_dict)

    frequency_expression.insert(1, hl.agg.call_stats(mt.GT, mt.alleles))
    meta_expressions.insert(1, {'group': 'raw'})

    print(f'Calculating {len(frequency_expression)} aggregators...')
    global_expression = {
        'freq_meta': meta_expressions
    }
    mt = mt.annotate_rows(freq=frequency_expression,
                          age_hist_het=hl.agg.hist(hl.agg.filter(mt.adj & mt.GT.is_het(), mt.age), 30, 80, 10),
                          age_hist_hom=hl.agg.hist(hl.agg.filter(mt.adj & mt.GT.is_hom_var(), mt.age), 30, 80, 10))
    if calculate_downsampling: global_expression['downsamplings'] = downsamplings
    mt = mt.annotate_globals(**global_expression)
    sample_data = mt.cols()

    pops = set(cut_data.pop.keys())
    [pops.discard(x) for x in POPS_TO_REMOVE_FOR_POPMAX]

    mt = mt.annotate_rows(popmax=add_popmax_expr(mt.freq, mt.freq_meta, populations=pops),
                          faf=add_faf_expr(mt.freq, mt.freq_meta, mt.locus, populations=pops))
    mt = get_projectmax(mt, mt.project_id)

    return mt.rows(), sample_data


def main(args):
    hl.init(log='/frequency_annotations.log')

    data_type = 'genomes' if args.genomes else 'exomes'

    mt = get_gnomad_data(data_type, release_samples=True)
    if args.subset_samples_by_field:
        mt = mt.filter_cols(mt.meta[args.subset_samples_by_field], keep=not args.invert)
    logger.info(f'Calculating frequencies for {mt.count_cols()} samples with {args.subset_samples_by_field} == {not args.invert}')
    ht, sample_table = generate_frequency_data(mt, args.downsampling, args.by_platform)

    location = f'frequencies_{args.subset_samples_by_field}' if args.subset_samples_by_field else 'frequencies'
    write_temp_gcs(ht, annotations_ht_path(data_type, location), args.overwrite)
    if args.downsampling:
        sample_table.write(sample_annotations_table_path(data_type, 'downsampling'), args.overwrite)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exomes', help='Input MT is exomes. One of --exomes or --genomes is required.', action='store_true')
    parser.add_argument('--genomes', help='Input MT is genomes. One of --exomes or --genomes is required.', action='store_true')
    parser.add_argument('--downsampling', help='Also calculate downsampling frequency data', action='store_true')
    parser.add_argument('--by_platform', help='Also calculate frequencies by platform', action='store_true')
    parser.add_argument('--subset_samples_by_field', help='Annotation field containing boolean values describing samples to keep in subset')
    parser.add_argument('--invert', help='Remove samples in --subset_samples_by_field instead of keeping (e.g. neuro)', action='store_true')
    parser.add_argument('--slack_channel', help='Slack channel to post results and notifications to.')
    parser.add_argument('--overwrite', help='Overwrite data', action='store_true')
    args = parser.parse_args()

    if int(args.exomes) + int(args.genomes) != 1:
        sys.exit('Error: One and only one of --exomes or --genomes must be specified')

    if args.slack_channel:
        try_slack(args.slack_channel, main, args)
    else:
        main(args)
