from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Union

import pharmpy.tools.iivsearch.algorithms as algorithms
from pharmpy.deps import pandas as pd
from pharmpy.model import Model, Results
from pharmpy.modeling import add_pk_iiv, calculate_bic, copy_model, create_joint_distribution
from pharmpy.modeling.results import RANK_TYPES
from pharmpy.tools import summarize_modelfit_results
from pharmpy.tools.common import create_results
from pharmpy.tools.modelfit import create_fit_workflow
from pharmpy.utils import runtime_type_check, same_arguments_as
from pharmpy.workflows import Task, ToolDatabase, Workflow, call_workflow

IIV_STRATEGIES = frozenset(('no_add', 'add_diagonal', 'fullblock'))
IIV_ALGORITHMS = frozenset(('brute_force',) + tuple(dir(algorithms)))


@dataclass
class State:
    algorithm: str
    model_names_so_far: Set[str]
    input_model_name: List[str]


def create_workflow(
    algorithm: str,
    iiv_strategy: str = 'no_add',
    rank_type: str = 'bic',
    cutoff: Optional[Union[float, int]] = None,
    model: Optional[Model] = None,
):
    """Run IIVsearch tool. For more details, see :ref:`iivsearch`.

    Parameters
    ----------
    algorithm : str
        Which algorithm to run (brute_force, brute_force_no_of_etas, brute_force_block_structure)
    iiv_strategy : str
        If/how IIV should be added to start model. Possible strategies are 'no_add', 'add_diagonal',
        or 'fullblock'. Default is 'no_add'
    rank_type : str
        Which ranking type should be used (OFV, AIC, BIC). Default is BIC
    cutoff : float
        Cutoff for which value of the ranking function that is considered significant. Default
        is None (all models will be ranked)
    model : Model
        Pharmpy model

    Returns
    -------
    IIVSearchResults
        IIVsearch tool result object

    Examples
    --------
    >>> from pharmpy.modeling import *
    >>> model = load_example_model("pheno")
    >>> from pharmpy.tools import run_iivsearch     # doctest: +SKIP
    >>> run_iivsearch('brute_force', model=model)   # doctest: +SKIP
    """

    wf = Workflow()
    wf.name = 'iivsearch'
    start_task = Task('start_iiv', start, model, algorithm, iiv_strategy, rank_type, cutoff)
    wf.add_task(start_task)
    task_results = Task('results', _results)
    wf.add_task(task_results, predecessors=[start_task])
    return wf


def create_algorithm_workflow(input_model, base_model, state, iiv_strategy, rank_type, cutoff):
    wf: Workflow[IIVSearchResults] = Workflow()

    start_task = Task(f'start_{state.algorithm}', _start_algorithm, base_model)
    wf.add_task(start_task)

    if iiv_strategy != 'no_add':
        wf_fit = create_fit_workflow(n=1)
        wf.insert_workflow(wf_fit)
        base_model_task = wf_fit.output_tasks[0]
    else:
        base_model_task = start_task

    index_offset = len(
        [model_name for model_name in state.model_names_so_far if 'base' not in model_name]
    )
    algorithm_func = getattr(algorithms, state.algorithm)
    wf_method = algorithm_func(base_model, index_offset)
    wf.insert_workflow(wf_method)

    task_result = Task(
        'results', post_process, state, rank_type, cutoff, input_model, base_model.name
    )

    post_process_tasks = [base_model_task] + wf.output_tasks
    wf.add_task(task_result, predecessors=post_process_tasks)

    return wf


def start(context, input_model, algorithm, iiv_strategy, rank_type, cutoff):
    if iiv_strategy != 'no_add':
        model_iiv = copy_model(input_model, 'base_model')
        _add_iiv(iiv_strategy, model_iiv)
        base_model = model_iiv
    else:
        base_model = input_model

    if algorithm == 'brute_force':
        list_of_algorithms = ['brute_force_no_of_etas', 'brute_force_block_structure']
    else:
        list_of_algorithms = [algorithm]
    sum_tools, sum_models, sum_inds, sum_inds_count, sum_errs = [], [], [], [], []

    models = []
    models_set = set()
    last_res = None
    final_model = None

    for i, algorithm_cur in enumerate(list_of_algorithms):
        state = State(algorithm_cur, models_set, input_model.name)
        # NOTE Execute algorithm
        wf = create_algorithm_workflow(
            input_model, base_model, state, iiv_strategy, rank_type, cutoff
        )
        res = call_workflow(wf, f'results_{algorithm}', context)
        # NOTE Append results
        new_models = list(filter(lambda model: model.name not in models_set, res.models))
        models.extend(new_models)
        models_set.update(model.name for model in new_models)

        if i == 0:
            # Have input model as first row in summary of models as step 0
            sum_models.append(summarize_modelfit_results(input_model))

        sum_tools.append(res.summary_tool)
        sum_models.append(res.summary_models)
        sum_inds.append(res.summary_individuals)
        sum_inds_count.append(res.summary_individuals_count)
        sum_errs.append(res.summary_errors)

        final_model = next(
            filter(lambda model: model.name == res.final_model_name, res.models), base_model
        )

        base_model = final_model
        iiv_strategy = 'no_add'
        last_res = res

    assert last_res is not None
    assert final_model is not None

    res_modelfit_input = input_model.modelfit_results
    res_modelfit_final = final_model.modelfit_results
    if res_modelfit_input and res_modelfit_final:
        bic_input = calculate_bic(input_model, res_modelfit_input.ofv, type='iiv')
        bic_final = calculate_bic(final_model, res_modelfit_final.ofv, type='iiv')
        if bic_final > bic_input:
            warnings.warn(
                f'Worse {rank_type} in final model {final_model.name} '
                f'({bic_final}) than {input_model.name} ({bic_input}), selecting '
                f'input model'
            )
            last_res.final_model_name = input_model.name

    keys = list(range(1, len(list_of_algorithms) + 1))

    res = IIVSearchResults(
        summary_tool=_concat_summaries(sum_tools, keys),
        summary_models=_concat_summaries(sum_models, [0] + keys),  # To include input model
        summary_individuals=_concat_summaries(sum_inds, keys),
        summary_individuals_count=_concat_summaries(sum_inds_count, keys),
        summary_errors=_concat_summaries(sum_errs, keys),
        final_model_name=last_res.final_model_name,
        models=models,
        tool_database=last_res.tool_database,
    )

    return res


def _concat_summaries(summaries, keys):
    return pd.concat(summaries, keys=keys, names=['step'])


def _results(res):
    return res


def _start_algorithm(model):
    model.parent_model = model.name
    return model


def _add_iiv(iiv_strategy, model):
    assert iiv_strategy in ['add_diagonal', 'fullblock']
    add_pk_iiv(model)
    if iiv_strategy == 'fullblock':
        create_joint_distribution(
            model, individual_estimates=model.modelfit_results.individual_estimates
        )
    return model


def post_process(state, rank_type, cutoff, input_model, base_model_name, *models):
    res_models = []
    base_model = None
    for model in models:
        if model.name == base_model_name:
            base_model = model
        else:
            res_models.append(model)

    if not base_model:
        raise ValueError('Error in workflow: No base model')

    res = create_results(
        IIVSearchResults, input_model, base_model, res_models, rank_type, cutoff, bic_type='iiv'
    )

    res.summary_tool['algorithm'] = state.algorithm
    # If base model is model from a previous step or is the input model to the full tool,
    # it should be excluded in this step
    if base_model_name in state.model_names_so_far or base_model_name == state.input_model_name:
        res.summary_models = summarize_modelfit_results(res_models)
    else:
        res.summary_models = summarize_modelfit_results(models)

    return res


@runtime_type_check
@same_arguments_as(create_workflow)
def validate_input(
    algorithm,
    iiv_strategy,
    rank_type,
):
    if algorithm not in IIV_ALGORITHMS:
        raise ValueError(
            f'Invalid `algorithm`: got `{algorithm}`, must be one of {sorted(IIV_ALGORITHMS)}.'
        )

    if rank_type not in RANK_TYPES:
        raise ValueError(
            f'Invalid `rank_type`: got `{rank_type}`, must be one of {sorted(RANK_TYPES)}.'
        )

    if iiv_strategy not in IIV_STRATEGIES:
        raise ValueError(
            f'Invalid `iiv_strategy`: got `{iiv_strategy}`,'
            f' must be one of {sorted(IIV_STRATEGIES)}.'
        )


@dataclass
class IIVSearchResults(Results):
    summary_tool: pd.DataFrame
    summary_individuals: pd.DataFrame
    summary_individuals_count: pd.DataFrame
    summary_errors: pd.DataFrame
    final_model_name: Optional[str] = None
    models: Sequence[Model] = ()
    summary_models: Optional[pd.DataFrame] = None  # NOTE Not present in Results
    tool_database: Optional[ToolDatabase] = None  # NOTE Not present in Results
