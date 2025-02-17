from __future__ import annotations

import importlib
import inspect
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

import pharmpy
import pharmpy.results
import pharmpy.tools.modelfit
from pharmpy.deps import numpy as np
from pharmpy.deps import pandas as pd
from pharmpy.model import Model, Results
from pharmpy.modeling import (
    calculate_aic,
    calculate_bic,
    check_high_correlations,
    copy_model,
    read_model_from_database,
)
from pharmpy.modeling.lrt import degrees_of_freedom as lrt_df
from pharmpy.modeling.lrt import test as lrt_test
from pharmpy.tools.psn_helpers import create_results as psn_create_results
from pharmpy.utils import normalize_user_given_path
from pharmpy.workflows import execute_workflow, split_common_options
from pharmpy.workflows.model_database import LocalModelDirectoryDatabase, ModelDatabase
from pharmpy.workflows.tool_database import ToolDatabase


def fit(
    model_or_models: Union[Model, List[Model]], tool: Optional[str] = None
) -> Union[Model, List[Model]]:
    """Fit models.

    Parameters
    ----------
    model_or_models : Model | list[Model]
        List of models or one single model
    tool : str
        Estimation tool to use. None to use default

    Return
    ------
    Model | list[Model]
        Input model or models with model fit results

    Examples
    --------
    >>> from pharmpy.modeling import load_example_model
    >>> from pharmpy.tools import fit
    >>> model = load_example_model("pheno")      # doctest: +SKIP
    >>> fit(model)      # doctest: +SKIP

    See also
    --------
    run_tool

    """
    single, models = (
        (True, [model_or_models])
        if isinstance(model_or_models, Model)
        else (False, model_or_models)
    )

    kept = []
    # Do not fit model if already fit
    for model in models:
        try:
            db_model = read_model_from_database(model.name, database=model.database)
        except (KeyError, AttributeError):
            db_model = None
        if (
            db_model
            and db_model.modelfit_results is not None
            and db_model == model
            and model.has_same_dataset_as(db_model)
        ):
            model.modelfit_results = db_model.modelfit_results
        else:
            kept.append(model)

    if kept:
        run_tool('modelfit', kept, tool=tool)

    return models[0] if single else models


def create_results(path, **kwargs):
    """Create/recalculate results object given path to run directory

    Parameters
    ----------
    path : str, Path
        Path to run directory
    kwargs
        Arguments to pass to tool specific create results function

    Returns
    -------
    Results
        Results object for tool

    Examples
    --------
    >>> from pharmpy.tools import create_results
    >>> res = create_results("frem_dir1")   # doctest: +SKIP

    See also
    --------
    read_results

    """
    path = normalize_user_given_path(path)
    res = psn_create_results(path, **kwargs)
    return res


def read_results(path):
    """Read results object from file

    Parameters
    ----------
    path : str, Path
        Path to results file

    Return
    ------
    Results
        Results object for tool

    Examples
    --------
    >>> from pharmpy.tools import read_results
    >>> res = read_results("results.json")     # doctest: +SKIP

    See also
    --------
    create_results

    """
    path = normalize_user_given_path(path)
    res = pharmpy.results.read_results(path)
    return res


def run_tool(name, *args, **kwargs) -> Union[Model, List[Model], Tuple[Model], Results]:
    """Run tool workflow

    Parameters
    ----------
    name : str
        Name of tool to run
    args
        Arguments to pass to tool
    kwargs
        Arguments to pass to tool

    Return
    ------
    Results
        Results object for tool

    Examples
    --------
    >>> from pharmpy.modeling import *
    >>> model = load_example_model("pheno")
    >>> from pharmpy.tools import run_tool # doctest: +SKIP
    >>> res = run_tool("ruvsearch", model)   # doctest: +SKIP

    """
    tool = importlib.import_module(f'pharmpy.tools.{name}')
    common_options, tool_options = split_common_options(kwargs)

    tool_params = inspect.signature(tool.create_workflow).parameters
    tool_metadata = _create_metadata_tool(name, tool_params, tool_options, args)

    if validate_input := getattr(tool, 'validate_input', None):
        validate_input(*args, **tool_options)

    wf = tool.create_workflow(*args, **tool_options)

    dispatcher, database = _get_run_setup(common_options, wf.name)
    setup_metadata = _create_metadata_common(common_options, dispatcher, database, wf.name)
    tool_metadata['common_options'] = setup_metadata
    database.store_metadata(tool_metadata)

    if name != 'modelfit':
        _store_input_models(list(args) + list(kwargs.items()), database)

    res = execute_workflow(wf, dispatcher=dispatcher, database=database)
    assert name == 'modelfit' or isinstance(res, Results)

    tool_metadata['stats']['end_time'] = _now()
    database.store_metadata(tool_metadata)

    return res


def _store_input_models(args, database):
    input_models = _get_input_models(args)

    if len(input_models) == 1:
        _create_input_model(input_models[0], database)
    else:
        for i, model in enumerate(input_models, 1):
            _create_input_model(model, database, number=i)


def _get_input_models(args):
    input_models = []
    for arg in args:
        if isinstance(arg, Model):
            input_models.append(arg)
        else:
            arg_as_list = [a for a in arg if isinstance(a, Model)]
            input_models.extend(arg_as_list)
    return input_models


def _create_input_model(model, tool_db, number=None):
    input_name = 'input_model'
    if number is not None:
        input_name += str(number)
    model_copy = copy_model(model, input_name)
    with tool_db.model_database.transaction(model_copy) as txn:
        txn.store_model()
        txn.store_modelfit_results()


def _now():
    return datetime.now().astimezone().isoformat()


def _create_metadata_tool(tool_name, tool_params, tool_options, args):
    # FIXME: add config file dump, estimation tool etc.
    tool_metadata = {
        'pharmpy_version': pharmpy.__version__,
        'tool_name': tool_name,
        'stats': {'start_time': _now()},
        'tool_options': dict(),
    }

    for i, p in enumerate(tool_params.values()):
        # Positional args
        if p.default == p.empty:
            try:
                name, value = p.name, args[i]
            except IndexError:
                try:
                    name, value = p.name, tool_options[p.name]
                except KeyError:
                    raise ValueError(f'{tool_name}: \'{p.name}\' was not set')
        # Named args
        else:
            if p.name in tool_options.keys():
                name, value = p.name, tool_options[p.name]
            else:
                name, value = p.name, p.default
        if isinstance(value, Model):
            value = str(value)  # FIXME: better model representation
        tool_metadata['tool_options'][name] = value

    return tool_metadata


def _create_metadata_common(common_options, dispatcher, database, toolname):
    setup_metadata = dict()
    setup_metadata['dispatcher'] = dispatcher.__name__
    # FIXME: naming of workflows/tools should be consistent (db and input name of tool)
    setup_metadata['database'] = {
        'class': type(database).__name__,
        'toolname': toolname,
        'path': str(database.path),
    }
    for key, value in common_options.items():
        if key not in setup_metadata.keys():
            if isinstance(value, Path):
                value = str(value)
            setup_metadata[str(key)] = value

    return setup_metadata


def _get_run_setup(common_options, toolname):
    try:
        dispatcher = common_options['dispatcher']
    except KeyError:
        from pharmpy.workflows import default_dispatcher

        dispatcher = default_dispatcher

    try:
        database = common_options['database']
    except KeyError:
        from pharmpy.workflows import default_tool_database

        if 'path' in common_options.keys():
            path = common_options['path']
        else:
            path = None
        database = default_tool_database(
            toolname=toolname, path=path, exist_ok=common_options.get('resume', False)
        )  # TODO: database -> tool_database

    return dispatcher, database


def retrieve_models(source, names=None):
    """Retrieve models after a tool run

    Any models created and run by the tool can be
    retrieved.

    Parameters
    ----------
    source : str, Path, Results, ToolDatabase, ModelDatabase
        Source where to find models. Can be a path (as str or Path), a results object, or a
        ToolDatabase/ModelDatabase
    names : list
        List of names of the models to retrieve or None for all

    Return
    ------
    list
        List of retrieved model objects

    Examples
    --------
    >>> from pharmpy.tools import retrieve_models
    >>> tooldir_path = 'path/to/tool/directory'
    >>> models = retrieve_models(tooldir_path, names=['run1'])      # doctest: +SKIP

    See also
    --------
    retrieve_final_model

    """
    if isinstance(source, Path) or isinstance(source, str):
        path = Path(source)
        # FIXME: Should be using metadata to know how to init databases
        db = LocalModelDirectoryDatabase(path / 'models')
    elif isinstance(source, Results):
        if hasattr(source, 'tool_database'):
            db = source.tool_database.model_database
        else:
            raise ValueError(
                f'Results type \'{source.__class__.__name__}\' does not serialize tool database'
            )
    elif isinstance(source, ToolDatabase):
        db = source.model_database
    elif isinstance(source, ModelDatabase):
        db = source
    else:
        raise NotImplementedError(f'Not implemented for type \'{type(source)}\'')
    names_all = db.list_models()
    if names is None:
        names = names_all
    diff = set(names).difference(names_all)
    if diff:
        raise ValueError(f'Models {diff} not in database')
    models = [db.retrieve_model(name) for name in names]
    return models


def retrieve_final_model(res):
    """Retrieve final model from a result object

    Parameters
    ----------
    res : Results
        A results object

    Return
    ------
    Model
        Reference to final model

    Examples
    --------
    >>> from pharmpy.tools import read_results, retrieve_final_model
    >>> res = read_results("results.json")     # doctest: +SKIP
    >>> model = retrieve_final_model(res)      # doctest: +SKIP

    See also
    --------
    retrieve_models

    """
    if res.final_model_name is None:
        raise ValueError('Attribute \'final_model_name\' is None')
    return retrieve_models(res, names=[res.final_model_name])[0]


def print_fit_summary(model):
    """Print a summary of the model fit

    Parameters
    ----------
    model : Model
        Pharmpy model object
    """

    def bool_ok_error(x):
        return "OK" if x else "ERROR"

    def bool_yes_no(x):
        return "YES" if x else "NO"

    def print_header(text, first=False):
        if not first:
            print()
        print(text)
        print("-" * len(text))

    def print_fmt(text, result):
        print(f"{text:33} {result}")

    res = model.modelfit_results

    print_header("Parameter estimation status", first=True)
    print_fmt("Minimization successful", bool_ok_error(res.minimization_successful))
    print_fmt("No rounding errors", bool_ok_error(res.termination_cause != 'rounding_errors'))
    print_fmt("Objective function value", round(res.ofv, 1))

    print_header("Parameter uncertainty status")
    cov_run = model.estimation_steps[-1].cov
    print_fmt("Covariance step run", bool_yes_no(cov_run))

    if cov_run:
        condno = round(np.linalg.cond(res.correlation_matrix), 1)
        print_fmt("Condition number", condno)
        print_fmt("Condition number < 1000", bool_ok_error(condno < 1000))
        cor = model.modelfit_results.correlation_matrix
        hicorr = check_high_correlations(model, cor)
        print_fmt("No correlations arger than 0.9", bool_ok_error(hicorr.empty))

    print_header("Parameter estimates")
    pe = res.parameter_estimates
    if cov_run:
        se = res.standard_errors
        rse = se / pe
        rse.name = 'RSE'
        df = pd.concat([pe, se, rse], axis=1)
    else:
        df = pd.concat([pe], axis=1)
    print(df)


def write_results(results, path, lzma=False, csv=False):
    """Write results object to json (or csv) file

    Note that the csv-file cannot be read into a results object again.

    Parameters
    ----------
    results : Results
        Pharmpy results object
    path : Path
        Path to results file
    lzma : bool
        True for lzma compression. Not applicable to csv file
    csv : bool
        Save as csv file
    """
    if csv:
        results.to_csv(path)
    else:
        results.to_json(path, lzma=lzma)


def summarize_errors(models):
    """Summarize errors and warnings from one or multiple model runs.

    Summarize the errors and warnings found after running the model/models.

    Parameters
    ----------
    models : list, Model
        List of models or single model

    Return
    ------
    pd.DataFrame
        A DataFrame of errors with model name, category (error or warning), and an int as index,
        an empty DataFrame if there were no errors or warnings found.

    Examples
    --------
    >>> from pharmpy.modeling import load_example_model
    >>> from pharmpy.tools import summarize_errors
    >>> model = load_example_model("pheno")
    >>> summarize_errors(model)      # doctest: +SKIP
    """
    # FIXME: have example with errors
    if isinstance(models, Model):
        models = [models]

    idcs, rows = [], []

    for model in models:
        res = model.modelfit_results
        if res is not None and len(res.log.log) > 0:
            for i, entry in enumerate(res.log.log):
                idcs.append((model.name, entry.category, i))
                rows.append([entry.time, entry.message])

    index_names = ['model', 'category', 'error_no']
    col_names = ['time', 'message']
    index = pd.MultiIndex.from_tuples(idcs, names=index_names)

    if rows:
        df = pd.DataFrame(rows, columns=col_names, index=index)
    else:
        df = pd.DataFrame(columns=col_names, index=index)

    return df.sort_index()


def rank_models(
    base_model, models, errors_allowed=None, rank_type='ofv', cutoff=None, bic_type='mixed'
) -> pd.DataFrame:
    """Ranks a list of models

    Ranks a list of models with a given ranking function

    Parameters
    ----------
    base_model : Model
        Base model to compare to
    models : list
        List of models
    errors_allowed : list or None
        List of errors that are allowed for ranking. Currently available is: rounding_errors and
        maxevals_exceeded. Default is None
    rank_type : str
        Name of ranking type. Available options are 'ofv', 'aic', 'bic', 'lrt' (OFV with LRT)
    cutoff : float or None
        Value to use as cutoff. If using LRT, cutoff denotes p-value. Default is None
    bic_type : str
        Type of BIC to calculate. Default is the mixed effects.

    Return
    ------
    pd.DataFrame
        DataFrame of the ranked models

    Examples
    --------
    >>> from pharmpy.modeling import load_example_model
    >>> from pharmpy.tools import rank_models
    >>> model_1 = load_example_model("pheno")
    >>> model_2 = load_example_model("pheno_linear")
    >>> rank_models(model_1, [model_2],
    ...             errors_allowed=['rounding_errors'],
    ...             rank_type='lrt') # doctest: +SKIP
    """
    models_all = [base_model] + models

    rank_values, delta_values = {}, {}
    models_to_rank = []

    ref_value = _get_rankval(base_model, rank_type, bic_type)
    model_dict = {model.name: model for model in models_all}

    # Filter on strictness
    for model in models_all:
        # Exclude OFV etc. if model was not successful
        if not model.modelfit_results or np.isnan(model.modelfit_results.ofv):
            continue
        if not model.modelfit_results.minimization_successful:
            if errors_allowed:
                if model.modelfit_results.termination_cause not in errors_allowed:
                    continue
                if np.isnan(model.modelfit_results.significant_digits):
                    continue
            else:
                continue

        rank_value = _get_rankval(model, rank_type, bic_type)
        if rank_type == 'lrt':
            parent = model_dict[model.parent_model]
            if cutoff is None:
                co = 0.05 if lrt_df(parent, model) >= 0 else 0.01
            elif isinstance(cutoff, tuple):
                co = cutoff[0] if lrt_df(parent, model) >= 0 else cutoff[1]
            else:
                assert isinstance(cutoff, (float, int))
                co = cutoff
            parent_ofv = np.nan if (mfr := parent.modelfit_results) is None else mfr.ofv
            model_ofv = np.nan if (mfr := model.modelfit_results) is None else mfr.ofv
            if not lrt_test(parent, model, parent_ofv, model_ofv, co):
                continue
        elif cutoff is not None:
            if ref_value - rank_value <= cutoff:
                continue

        # Add ranking value and model
        rank_values[model.name] = rank_value
        delta_values[model.name] = ref_value - rank_value
        models_to_rank.append(model)

    # Sort
    def _get_delta(model):
        if np.isnan(ref_value):
            return -rank_values[model.name]
        return delta_values[model.name]

    models_sorted = sorted(models_to_rank, key=_get_delta, reverse=True)

    # Create rank for models, if two have the same value they will have the same rank
    ranking = dict()
    rank, count, prev = 0, 0, None
    for model in models_sorted:
        count += 1
        value = _get_delta(model)
        if value != prev:
            rank += count
            prev = value
            count = 0
        ranking[model.name] = rank

    rows = dict()
    for model in models_all:
        delta, rank_value, rank = np.nan, np.nan, np.nan
        if model.name in ranking.keys():
            rank = ranking[model.name]
        if model.name in rank_values.keys():
            rank_value = rank_values[model.name]
        if model.name in delta_values.keys():
            delta = delta_values[model.name]

        rows[model.name] = (delta, rank_value, rank)

    if rank_type == 'lrt':
        rank_type_name = 'ofv'
    else:
        rank_type_name = rank_type

    index = pd.Index(rows.keys(), name='model')
    df = pd.DataFrame(
        rows.values(), index=index, columns=[f'd{rank_type_name}', f'{rank_type_name}', 'rank']
    )

    if np.isnan(ref_value):
        return df.sort_values(by=[f'{rank_type_name}'])
    else:
        return df.sort_values(by=[f'd{rank_type_name}'], ascending=False)


def _get_rankval(model, rank_type, bic_type):
    if not model.modelfit_results:
        return np.nan
    if rank_type in ['ofv', 'lrt']:
        return model.modelfit_results.ofv
    elif rank_type == 'aic':
        return calculate_aic(model, model.modelfit_results.ofv)
    elif rank_type == 'bic':
        return calculate_bic(model, model.modelfit_results.ofv, bic_type)
    else:
        raise ValueError('Unknown rank_type: must be ofv, lrt, aic, or bic')


def summarize_modelfit_results(models, include_all_estimation_steps=False):
    """Summarize results of model runs

    Summarize different results after fitting a model, includes runtime, ofv,
    and parameter estimates (with errors). If include_all_estimation_steps is False,
    only the last estimation step will be included (note that in that case, the
    minimization_successful value will be referring to the last estimation step, if
    last step is evaluation it will go backwards until it finds an estimation step
    that wasn't an evaluation).

    Parameters
    ----------
    models : list, Model
        List of models or single model
    include_all_estimation_steps : bool
        Whether to include all estimation steps, default is False

    Return
    ------
    pd.DataFrame
        A DataFrame of modelfit results with model name and estmation step as index.

    Examples
    --------
    >>> from pharmpy.modeling import load_example_model
    >>> from pharmpy.tools import summarize_modelfit_results
    >>> model = load_example_model("pheno")
    >>> summarize_modelfit_results(model) # doctest: +ELLIPSIS
                     description  minimization_successful ...        ofv  ... runtime_total  ...
    pheno PHENOBARB SIMPLE MODEL                     True ... 586.276056  ...           4.0  ...
    """
    # FIXME: add option for bic type?
    if isinstance(models, Model):
        models = [models]

    summaries = []

    for model in models:
        if model.modelfit_results is not None:
            summary = _get_model_result_summary(model, include_all_estimation_steps)
            summary.insert(0, 'description', model.description)
            summaries.append(summary)
        else:
            if include_all_estimation_steps:
                for i, est in enumerate(model.estimation_steps):
                    index = pd.MultiIndex.from_tuples(
                        [(model.name, i + 1)], names=['model', 'step']
                    )
                    if est.evaluation:
                        run_type = 'evaluation'
                    else:
                        run_type = 'estimation'
                    empty_df = pd.DataFrame({'run_type': run_type}, index=index)
                    summaries.append(empty_df)
            else:
                index = pd.Index([model.name], name='model')
                empty_df = pd.DataFrame(index=index)
                summaries.append(empty_df)

    df = pd.concat(summaries)

    return df


def _get_model_result_summary(model, include_all_estimation_steps=False):
    if not include_all_estimation_steps:
        summary_dict = _summarize_step(model, -1)
        index = pd.Index([model.name], name='model')
        summary_df = pd.DataFrame(summary_dict, index=index)
    else:
        summary_dicts = []
        tuples = []
        for i in range(len(model.estimation_steps)):
            summary_dict = _summarize_step(model, i)
            is_evaluation = model.estimation_steps[i].evaluation
            if is_evaluation:
                run_type = 'evaluation'
            else:
                run_type = 'estimation'
            summary_dict = {'run_type': run_type, **summary_dict}
            summary_dicts.append(summary_dict)
            tuples.append((model.name, i + 1))
        index = pd.MultiIndex.from_tuples(tuples, names=['model', 'step'])
        summary_df = pd.DataFrame(summary_dicts, index=index)

    log_df = model.modelfit_results.log.to_dataframe()

    no_of_errors = len(log_df[log_df['category'] == 'ERROR'])
    no_of_warnings = len(log_df[log_df['category'] == 'WARNING'])

    minimization_idx = summary_df.columns.get_loc('minimization_successful')
    summary_df.insert(loc=minimization_idx + 1, column='errors_found', value=no_of_errors)
    summary_df.insert(loc=minimization_idx + 2, column='warnings_found', value=no_of_warnings)

    return summary_df


def _summarize_step(model, i):
    res = model.modelfit_results
    summary_dict = dict()

    if i >= 0:
        minsucc = res.minimization_successful_iterations.iloc[i]
    else:
        minsucc = res.minimization_successful

    if minsucc is not None:
        summary_dict['minimization_successful'] = minsucc
    else:
        summary_dict['minimization_successful'] = False

    if i == -1:
        i = max(res.ofv_iterations.index.get_level_values(0)) - 1
    ofv = res.ofv_iterations[
        i + 1,
    ].iloc[-1]
    summary_dict['ofv'] = ofv
    summary_dict['aic'] = calculate_aic(model, res.ofv)
    summary_dict['bic'] = calculate_bic(model, res.ofv)
    summary_dict['runtime_total'] = res.runtime_total
    summary_dict['estimation_runtime'] = res.estimation_runtime_iterations.iloc[i]

    pe = res.parameter_estimates_iterations.loc[
        i + 1,
    ].iloc[-1]
    ses = res.standard_errors
    rses = res.relative_standard_errors

    for param in pe.index:
        summary_dict[f'{param}_estimate'] = pe[param]
        if ses is not None:
            summary_dict[f'{param}_SE'] = ses[param]
        if rses is not None:
            summary_dict[f'{param}_RSE'] = rses[param]

    return summary_dict
