"""
An abstract FairTest Investigation.
"""

from .modules.context_discovery import tree_parser as tree_parser
from .modules.context_discovery import guided_tree as guided_tree
from .modules.statistics import multiple_testing as multitest
from .modules.bug_report import report as report_module
from .modules.bug_report import filter_rank as filter_rank
from .holdout import DataSource

import numpy as np
from copy import copy
from os import path
import sys
import abc
import logging
import random
import warnings


class Investigation(object):
    """
    An abstract FairTest investigation.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, data_source, protected, output, expl=None,
                 metrics=None, random_state=None, to_drop=None):
        """
        Initializes a generic FairTest Investigation.

        Parameters
        ----------
        data_source :
            the data source consisting of a training set and a holdout set
        protected :
            list of names of protected features
        output :
            name of output feature or features
        expl :
            name of explanatory feature
        metrics :
            dictionary of custom metrics indexed by a protected feature
        random_state :
            seed for random generators
        to_drop :
            features to drop from the training set
        """

        if not isinstance(data_source, DataSource):
            raise ValueError('data_source argument should be an instance of '
                             'DataSource')

        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError('metrics should be a dictionary')

        if not protected:
            raise ValueError('at least one protected feature must be specified')

        if not hasattr(protected, '__iter__'):
            raise ValueError('protected attribute must be an iterable')

        if not output:
            raise ValueError('at least one output feature must be specified')

        if data_source.encoders is None:
            self.encoders = {}
        else:
            self.encoders = data_source.encoders

        self.holdout = data_source.holdout
        self.metrics = metrics if metrics is not None else {}
        self.trained_trees = {}
        self.contexts = {}
        self.stats = {}
        self.train_params = {}
        self.test_params = {}
        self.display_params = {}
        self.feature_info = {}
        self.test_set_size = 0

        if random_state:
            self.random_state = random_state
        else:
            self.random_state = 0
        # ro.r('set.seed({})'.format(self.random_state))
        random.seed(self.random_state)

        self.train_set = data_source.train_data.copy()

        if to_drop is not None:
            for col in to_drop:
                self.train_set = self.train_set.drop(col, axis=1)

        # check if all protected features are available
        for sens in protected:
            if sens not in self.train_set.columns:
                raise ValueError('Feature %s not found' % sens)

        for target in np.asarray([output]).flatten():
            if target not in self.train_set.columns:
                raise ValueError('Target %s not found' % target)

        # set feature information
        expl = [] if expl is None else expl
        for col in self.train_set.columns.drop(output):
            ftype = 'sens' if col in protected \
                else 'expl' if col in expl \
                else 'context'
            arity = None if col not in self.encoders \
                else len(self.encoders[col].classes_)
            self.feature_info[col] = Feature(ftype, arity)

        # get the names of the sensitive features
        self.sens_features = [name for (name, f) in self.feature_info.items()
                              if f.ftype == 'sens']

        # get the name of the explanatory feature (if any)
        expl_list = [name for (name, f) in self.feature_info.items()
                     if f.ftype == 'expl']
        self.expl = expl_list[0] if expl_list else None

        # check if the output is categorical
        try:
            target_arity = len(self.encoders[output].classes_)
        except (KeyError, TypeError):
            target_arity = None
        self.output = Target(np.asarray([output]).flatten(), arity=target_arity)
        logging.info('Target Feature: %s' % self.output)

        # choose default metrics
        self.set_default_metrics()

    @abc.abstractmethod
    def set_default_metrics(self):
        """
        Sets default Metrics for each protected features, if not specified
        by the user
        """
        return

    def preprocess_test_data(self, data):
        """
        Applies a pre-processing stage to the testing data

        Parameters
        ----------
        data :
            the test data
        """
        return data


def train(investigations, max_depth=5, min_leaf_size=100,
          score_aggregation=guided_tree.ScoreParams.AVG, max_bins=10,
          subsample_frac=1.0):
    """
    Form hypotheses about discrimination contexts for each protected feature
    in each investigation

    Parameters
    ----------
    investigations :
        a list of investigations to be carried out

    max_depth :
        maximum tree generation depth. Maximum number of features that
        define a discrimination context

    min_min_leaf_size :
        minimum size of a tree leaf. Minimum size of a discrimination
        context (on the training set)

    score_aggregation :
        method used to compute the score of a split. If "avg", averages the
        association scores of all the sub-populations generated by the
        split. If "weighted_avg", computes the average of the child scores
        weighted by the size of the sub-populations. If "max", returns
        the maximal association scores of all generated sub-populations.

    max_bins :
        maximum number of bins used for finding splits on continuous
        features
    """

    if max_depth < 0:
        raise ValueError('max_depth must be non-negative')
    if min_leaf_size <= 0:
        raise ValueError('min_leaf_size must be positive')
    if score_aggregation not in guided_tree.ScoreParams.AGG_TYPES:
        raise ValueError("score_aggregation should be one of 'avg', "
                         "'weighted_avg' or 'max', Got %s" % score_aggregation)
    if max_bins <= 0:
        raise ValueError('max_bins must be positive')

    if not hasattr(investigations, '__iter__'):
        raise ValueError('investigations must be an iterable')

    for inv in investigations:
        assert isinstance(inv, Investigation)
        if inv.train_set is None:
            raise RuntimeError('Investigation was not initialized')

    for inv in investigations:
        data = inv.train_set.copy()

        inv.train_params = {'max_depth': max_depth,
                            'min_leaf_size': min_leaf_size,
                            'agg_type': score_aggregation,
                            'max_bins': max_bins}

        # find discrimination contexts for each sensitive feature
        for sens in inv.sens_features:
            logging.info('Begin training phase with protected '
                         'feature %s' % sens)

            conf = inv.holdout.test_set_conf

            tree = guided_tree.build_tree(data, inv.feature_info, sens,
                                          inv.expl, inv.output,
                                          copy(inv.metrics[sens]),
                                          conf, max_depth, min_leaf_size,
                                          score_aggregation, max_bins,
                                          subsample_frac)
            inv.trained_trees[sens] = tree


def test(investigations, prune_insignificant=True, exact=True, correct=True,
         new_metrics=None, new_expl=None):
    """
    Compute effect sizes and p-values for the discrimination contexts
    discovered on the training set. Correct intervals and p-values across
    all investigations.

    Parameters
    ----------
    investigations :
            a list of investigations for which hypothesis tests should be
            performed

    prune_insignificant :
        if ``True``, prune discrimination contexts for which the effect
        on the training set is not statistically significant

    exact :
        if ``False``, approximate asymptotically correct methods are used to
        generate p-values and confidence intervals. Otherwise, confidence
        intervals are generated with bootstrapping techniques and p-values
        via Monte-Carlo permutation tests.
    """

    if not hasattr(investigations, '__iter__'):
        raise ValueError('investigations must be an iterable')

    if new_metrics is None:
        new_metrics = [{}]*len(investigations)

    if not len(new_metrics) == len(investigations):
        raise ValueError('new_metrics should be None, or a list of '
                         'dictionaries for each investigation')

    if new_expl is None:
        new_expl = [None]*len(investigations)

    if not len(new_expl) == len(investigations):
        raise ValueError('new_expl should be None, or a list of '
                         'features for each investigation')

    holdout = investigations[0].holdout

    for inv in investigations:
        assert isinstance(inv, Investigation)
        if not inv.trained_trees:
            raise RuntimeError('Investigation was not trained')

        if inv.holdout != holdout:
            raise RuntimeError('All tested investigations should use the same '
                               'holdout set')

    test_data = holdout.get_test_set()

    if 1: #try:
        for (idx, inv) in enumerate(investigations):
            inv.test_params = {'prune_insignificant': prune_insignificant,
                               'exact': exact,
                               'family_conf': inv.holdout.test_set_conf}

            data = test_data.copy()
            data = inv.preprocess_test_data(data)

            inv.test_set_size = len(data)

            if new_expl[idx] is not None:
                inv.expl = new_expl[idx]

            # prepare testing data for all hypotheses
            for sens in inv.sens_features:
                new_metric = new_metrics[idx].get(sens, None)
                if isinstance(new_metric, str):
                    new_metric = metric_from_string(new_metric)

                tree = inv.trained_trees[sens]
                logging.info('Parsing tree for sensitive feature %s...' % sens)
                inv.contexts[sens] = tree_parser.find_contexts(tree, data, inv.feature_info,
                                                               sens, inv.expl, inv.output,
                                                               prune_insignificant,
                                                               new_metric=new_metric)
                logging.info('Parsed tree for sensitive feature %s' % sens)

        # compute p-values and confidence intervals with FWER correction
        logging.info('Begin testing phase')
        np.random.seed(investigations[0].random_state)

        multitest.compute_all_stats(investigations, exact,
                                    holdout.test_set_conf, correct)
    else : #except Exception as e:
        holdout.return_unused_data(test_data)


def report(investigations, dataname, output_dir=None, filter_conf=0.95,
           node_filter=filter_rank.FILTER_BETTER_THAN_ANCESTORS):
    """
    Output a FairTest bug report for each protected feature in each
    investigation.

    Parameters
    ----------
    investigations :
        a list of investigations to report on

    dataname :
        name of the dataset used in the experiments

    output_dir :
        directory to which bug reports shall be output.
        Should be an absolute path. Default is None and
        reports are sent to stdout.

    filter_conf :
        confidence level for filtering out bugs. Filters out bugs for which the
        p-value is larger than (1-filter_conf). If filter_conf is set to 0, all
        bugs are retained

    node_filter :
        method used to filter bugs in each report. Bugs that are not
        statistically significant for the provided false discovery rate are
        filtered out automatically. Setting this parameter to
        "better_than_ancestors" additionally filters out a context if it
        does not exhibit a stronger association than the larger contexts that
        it is part of.
    """
    if not 0 <= filter_conf < 1:
        raise ValueError('filter_conf should be in [0,1), Got %s' % filter_conf)

    if node_filter not in filter_rank.NODE_FILTERS:
        raise ValueError("node_filter should be one of 'all', "
                         "'leaves', 'root' or 'better_than_ancestors',"
                         " Got %s" % node_filter)

    if not hasattr(investigations, '__iter__'):
        raise ValueError('investigations must be an iterable')

    for inv in investigations:
        assert isinstance(inv, Investigation)
        if not inv.stats:
            raise RuntimeError("Investigation was not tested")

    for idx, inv in enumerate(investigations):

        inv.display_params = {'node_filter': node_filter}

        if not output_dir:
            output_stream = sys.stdout
        elif not path.isdir(output_dir):
            raise IOError("Directory \"%s\" does not exist" % output_dir)
        else:
            if len(investigations) > 1:
                filename = path.join(output_dir, "report_" + dataname + "_" +
                                     str(idx) + ".txt")
            else:
                filename = path.join(output_dir, "report_" + dataname + ".txt")
            output_stream = open(filename, "w+")

        # print some global information about the investigation
        train_size = len(inv.train_set)
        test_size = inv.test_set_size
        sensitive = inv.sens_features
        contextual = [name for (name, f) in inv.feature_info.items()
                      if f.ftype == 'context']

        report_module.print_report_info(dataname, train_size, test_size,
                                        sensitive, contextual, inv.expl,
                                        inv.output.names, inv.train_params,
                                        inv.test_params, inv.display_params,
                                        output_stream)

        plot_dir = None
        sub_plot_dir = None
        if output_dir:
            if len(investigations) > 1:
                plot_dir = path.join(output_dir, dataname +
                                     "_" + str(idx) + "_plots")
            else:
                plot_dir = path.join(output_dir, dataname + "_plots")

        # print all the bug reports
        for sens in inv.sens_features:
            output_stream.write('Report of associations of O={} on Si = ' \
                                    '{}:'.format(inv.output.short_names, sens))
            output_stream.write('Association metric: {}'.format(inv.metrics[sens]))
            output_stream.write('')
            stats = inv.stats[sens]
            contexts = inv.contexts[sens]

            if plot_dir:
                if len(inv.sens_features) > 1:
                    sub_plot_dir = path.join(plot_dir, sens)
                else:
                    sub_plot_dir = plot_dir

            # dirty nasty hack for the benchmark
            txt = report_module.bug_report(contexts, stats, sens, inv.expl,
                                           inv.output, output_stream,
                                           conf=filter_conf,
                                           encoders=inv.encoders,
                                           node_filter=node_filter,
                                           plot_dir=sub_plot_dir)

            if len(inv.sens_features) == 1:
                if output_dir:
                    output_stream.close()
                return txt

        if output_dir:
            output_stream.close()


class Feature(object):
    """
    Holds information about a user feature
    """

    # types of user features
    TYPES = ['context', 'sens', 'expl']

    def __init__(self, ftype, arity=None):
        assert ftype in Feature.TYPES
        self.ftype = ftype
        self.arity = arity

    def __repr__(self):
        return "%s(type=%s, arity=%s)" \
               % (self.__class__.__name__, self.ftype, self.arity)


class Target(object):
    """
    Holds information about the target feature(s)
    """
    def __init__(self, names, arity=None):
        self.names = names
        self.num_labels = len(names)
        self.arity = arity
        self.short_names = self.names if len(self.names) < 10 else \
            '[{} ... {}]'.format(self.names[0], self.names[-1])

    def __repr__(self):

        return "%s(names=%s, arity=%s)" \
               % (self.__class__.__name__, self.short_names, self.arity)


def metric_from_string(m_str, **kwargs):
    """
    Gets a Metric from its string representation

    Parameters
    ----------
    m_str :
        the string representation

    kwargs :
        additional arguments used for constructing a metric
    """
    if m_str == "NMI" or m_str == "MI":
        return NMI()
    elif m_str == "CORR":
        return CORR()
    elif m_str == "DIFF":
        return DIFF()
    elif m_str == "RATIO":
        return RATIO()
    elif m_str == "REGRESSION":
        return REGRESSION(topk=kwargs['topk'])
    elif m_str == "CondDIFF":
        return CondDIFF()
    elif m_str == "CondNMI":
        return CondNMI()
    elif m_str == "CondCorr":
        return CondCORR()
    raise ValueError('Unknown fairness Metric {}'.format(m_str))
