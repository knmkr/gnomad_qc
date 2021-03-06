from gnomad_hail import *
from gnomad_hail.resources.sample_qc import *
import hdbscan

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("unified_sample_qc_b")
logger.setLevel(logging.INFO)


def assign_platform_pcs(platform_pc_table: hl.Table, out_filepath: str, num_pcs: int = 9) -> hl.Table:
    """
    Function assumes that platform_pc_table contains columns named 'combined_sample', 'gross_platform' (known labels), 'callratePC<n>'

    :param Table platform_pc_table: Table containing samples and callrate PCs
    :param str out_filepath: filepath for tsv containing samples, callrate PCs, and imputed platform labels
    :param int num_pcs: number of callrate PCs to use in platform imputation
    :return: Table containing samples, callrate PCs, and imputed platform labels
    :rtype: Table
    """
    # Read and format data for clustering
    data = platform_pc_table.to_pandas()
    cols = ['PC' + str(i + 1) for i in range(num_pcs)]
    callrate_data = data[cols].as_matrix()
    logger.info('Assigning platforms to {} exome samples in MT...'.format(len(callrate_data)))

    # Cluster data
    clusterer = hdbscan.HDBSCAN(min_cluster_size=100)
    cluster_labels = clusterer.fit_predict(callrate_data)
    n_clusters = len(set(cluster_labels)) - (-1 in cluster_labels)  # NOTE: -1 is the label for noisy (un-classifiable) data points
    logger.info('Found {} unique platforms during platform imputation...'.format(n_clusters))

    data['qc_platform'] = cluster_labels
    with hl.hadoop_open(out_filepath, 'w') as out:
        data.to_csv(out, sep="\t", index=False)
    new_data = hl.import_table(out_filepath, impute=True, types={'qc_platform': hl.str}).key_by('data_type', 's')
    return new_data


def main(args):
    hl.init(log='/platform_pca.log')

    if not args.skip_prepare_data_for_platform_pca:
        # ~1 hour on 800 cores (3/8/18)
        logger.info('Preparing data for platform PCA...')
        mt = get_gnomad_data('exomes', adj=True, raw=False, meta_root=None, fam_root=None, split=False)
        mt = filter_to_autosomes(mt)
        intervals = hl.import_locus_intervals(evaluation_intervals_path)
        mt = mt.annotate_rows(interval=intervals[mt.locus].target)
        mt = mt.filter_rows(hl.is_defined(mt.interval) & (hl.len(mt.alleles) == 2))
        mt = mt.select_entries(GT=hl.or_missing(hl.is_defined(mt.GT), hl.struct()))
        callrate_mt = mt.group_rows_by(mt.interval).aggregate(callrate=hl.agg.fraction(hl.is_defined(mt.GT)))
        callrate_mt.write(exome_callrate_mt_path, args.overwrite)

    if not args.skip_run_platform_pca:
        logger.info('Running platform PCA...')
        qc_ht = hl.read_table(qc_ht_path('exomes', 'hard_filters')).key_by('s')
        callrate_mt = hl.read_matrix_table(exome_callrate_mt_path)
        callrate_mt = callrate_mt.filter_cols(hl.len(qc_ht[callrate_mt.col_key].hard_filters) == 0)
        callrate_mt = callrate_mt.annotate_entries(callrate=hl.int(callrate_mt.callrate > 0.25))
        # Center until Hail's PCA does it for you
        callrate_mt = callrate_mt.annotate_rows(mean_callrate=hl.agg.mean(callrate_mt.callrate))
        callrate_mt = callrate_mt.annotate_entries(callrate=callrate_mt.callrate - callrate_mt.mean_callrate)
        eigenvalues, scores, _ = hl.pca(callrate_mt.callrate, compute_loadings=False)
        logger.info('Eigenvalues: {}'.format(eigenvalues))
        # [731282566.2824697, 78687228.90071851, 43837650.51729764, 33969298.61827205, 26308703.539534636, 21102437.512725923, 16949828.555817757, 12994894.187041137, 8372332.274295175, 8128326.814388647]
        scores.write(exome_callrate_scores_ht_path)

    logger.info('Annotating with platform PCs and known platform annotations...')
    scores = hl.read_table(exome_callrate_scores_ht_path).annotate(data_type='exomes')
    platform_pcs = assign_platform_pcs(scores, qc_temp_data_prefix('exomes') + '.assigned_platform_pcs.txt.bgz')
    platform_pcs.write(qc_ht_path('exomes', 'platforms'), overwrite=args.overwrite)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--overwrite', help='Overwrite pre-existing data', action='store_true')
    parser.add_argument('--skip_prepare_data_for_platform_pca', help='Skip prepping data for platform imputation (assuming already done)', action='store_true')
    parser.add_argument('--skip_run_platform_pca', help='Skip platform PCA (assuming already done)', action='store_true')
    parser.add_argument('--slack_channel', help='Slack channel to post results and notifications to.')

    args = parser.parse_args()

    if args.slack_channel:
        try_slack(args.slack_channel, main, args)
    else:
        main(args)