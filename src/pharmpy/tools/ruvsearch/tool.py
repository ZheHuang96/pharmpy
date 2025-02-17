from functools import partial
from typing import List, Optional

from pharmpy.deps import pandas as pd
from pharmpy.deps import sympy
from pharmpy.deps.scipy import stats
from pharmpy.model import (
    Assignment,
    EstimationStep,
    EstimationSteps,
    Model,
    NormalDistribution,
    Parameter,
    Parameters,
    RandomVariables,
    Statements,
)
from pharmpy.modeling import (
    add_population_parameter,
    add_time_after_dose,
    create_symbol,
    get_mdv,
    set_combined_error_model,
    set_iiv_on_ruv,
    set_initial_estimates,
    set_power_on_ruv,
)
from pharmpy.modeling.error import remove_error_model, set_time_varying_error_model
from pharmpy.tools import (
    summarize_errors,
    summarize_individuals,
    summarize_individuals_count_table,
    summarize_modelfit_results,
)
from pharmpy.tools.common import summarize_tool, update_initial_estimates
from pharmpy.tools.modelfit import create_fit_workflow
from pharmpy.utils import runtime_type_check, same_arguments_as
from pharmpy.workflows import Task, Workflow, call_workflow

from .results import RUVSearchResults, calculate_results

SKIP = frozenset(('IIV_on_RUV', 'power', 'combined', 'time_varying'))


def create_workflow(
    model: Optional[Model] = None,
    groups: int = 4,
    p_value: float = 0.05,
    skip: Optional[List[str]] = None,
):
    """Run the ruvsearch tool. For more details, see :ref:`ruvsearch`.

    Parameters
    ----------
    model : Model
        Pharmpy model
    groups : int
        The number of bins to use for the time varying models
    p_value : float
        The p-value to use for the likelihood ratio test
    skip : list
        A list of models to not attempt.

    Returns
    -------
    RUVSearchResults
        Ruvsearch tool result object

    Examples
    --------
    >>> from pharmpy.modeling import *
    >>> model = load_example_model("pheno")
    >>> from pharmpy.tools import run_ruvsearch # doctest: +SKIP
    >>> run_ruvsearch(model=model)      # doctest: +SKIP

    """

    wf = Workflow()
    wf.name = "ruvsearch"
    start_task = Task('start_ruvsearch', start, model, groups, p_value, skip)
    wf.add_task(start_task)
    task_results = Task('results', _results)
    wf.add_task(task_results, predecessors=[start_task])
    return wf


def create_iteration_workflow(model, groups, cutoff, skip, current_iteration):
    wf = Workflow()

    start_task = Task('start_iteration', _start_iteration, model)
    wf.add_task(start_task)

    task_base_model = Task(
        'create_base_model', partial(_create_base_model, current_iteration=current_iteration)
    )
    wf.add_task(task_base_model, predecessors=start_task)

    tasks = []
    if 'IIV_on_RUV' not in skip:
        task_iiv = Task(
            'create_iiv_on_ruv_model',
            partial(_create_iiv_on_ruv_model, current_iteration=current_iteration),
        )
        tasks.append(task_iiv)
        wf.add_task(task_iiv, predecessors=task_base_model)

    if 'power' not in skip and 'combined' not in skip:
        task_power = Task(
            'create_power_model', partial(_create_power_model, current_iteration=current_iteration)
        )
        wf.add_task(task_power, predecessors=task_base_model)
        tasks.append(task_power)
        task_combined = Task(
            'create_combined_error_model',
            partial(_create_combined_model, current_iteration=current_iteration),
        )
        wf.add_task(task_combined, predecessors=task_base_model)
        tasks.append(task_combined)

    if 'time_varying' not in skip:
        for i in range(1, groups):
            tvar = partial(
                _create_time_varying_model, groups=groups, i=i, current_iteration=current_iteration
            )
            task = Task(f"create_time_varying_model{i}", tvar)
            tasks.append(task)
            wf.add_task(task, predecessors=task_base_model)

    fit_wf = create_fit_workflow(n=1 + len(tasks))
    wf.insert_workflow(fit_wf, predecessors=[task_base_model] + tasks)
    post_pro = partial(post_process, cutoff=cutoff, current_iteration=current_iteration)
    task_post_process = Task('post_process', post_pro)
    wf.add_task(task_post_process, predecessors=[start_task] + fit_wf.output_tasks)

    return wf


def start(context, model, groups, p_value, skip):
    cutoff = float(stats.chi2.isf(q=p_value, df=1))
    if skip is None:
        skip = []

    sum_models = []
    selected_models = [model]
    cwres_models = []
    tool_database = None
    last_iteration = 0
    for current_iteration in (1, 2, 3):
        last_iteration = current_iteration
        wf = create_iteration_workflow(model, groups, cutoff, skip, current_iteration)
        res, best_model, selected_model_name = call_workflow(
            wf, f'results{current_iteration}', context
        )
        if current_iteration == 1:
            sum_models.append(summarize_modelfit_results(model))
        sum_models.append(summarize_modelfit_results(best_model))

        cwres_models.append(res.cwres_models)
        tool_database = res.tool_database

        if not selected_model_name.startswith('base'):
            selected_models.append(best_model)

        model = best_model

        if selected_model_name.startswith('base'):
            break
        elif selected_model_name.startswith('time_varying'):
            skip.append('time_varying')
        else:
            skip.append(selected_model_name)

    sumind = summarize_individuals(selected_models)
    sumcount = summarize_individuals_count_table(df=sumind)
    summf = pd.concat(sum_models, keys=list(range(last_iteration)), names=['step'])
    summary_tool = _create_summary_tool(selected_models, cutoff)
    summary_errors = summarize_errors(selected_models)

    res = RUVSearchResults(
        cwres_models=pd.concat(cwres_models),
        summary_individuals=sumind,
        summary_individuals_count=sumcount,
        final_model_name=model.name,
        summary_models=summf,
        summary_tool=summary_tool,
        summary_errors=summary_errors,
        tool_database=tool_database,
    )
    return res


def _create_summary_tool(selected_models, cutoff):
    model_names = [model.name for model in selected_models]
    iteration_map = {model.name: model_names.index(model.name) for model in selected_models}

    base_model = selected_models[0]
    ruvsearch_models = selected_models[1:]

    sum_tool = summarize_tool(ruvsearch_models, base_model, 'ofv', cutoff).reset_index()
    sum_tool['step'] = sum_tool['model'].map(iteration_map)
    sum_tool_by_iter = sum_tool.set_index(['step', 'model']).sort_index()

    # FIXME workaround since rank_models will exclude ranking of base model since dofv will be 0
    sum_tool_by_iter.loc[(0, base_model.name), 'ofv'] = base_model.modelfit_results.ofv
    sum_tool_by_iter.loc[(0, base_model.name), 'dofv'] = 0

    return sum_tool_by_iter.drop(columns=['rank'])


def _start_iteration(model):
    return model


def _results(res):
    return res


def post_process(context, start_model, *models, cutoff, current_iteration):
    res = calculate_results(models)
    best_model_unfitted, selected_model_name = _create_best_model(
        start_model, res, current_iteration, cutoff=cutoff
    )
    if best_model_unfitted is not None:
        fit_wf = create_fit_workflow(models=[best_model_unfitted])
        best_model = call_workflow(fit_wf, f'fit{current_iteration}', context)
        delta_ofv = start_model.modelfit_results.ofv - best_model.modelfit_results.ofv
        if delta_ofv > cutoff:
            return (res, best_model, selected_model_name)

    return (res, start_model, f"base_{current_iteration}")


def _create_base_model(input_model, current_iteration):
    base_model = Model()
    theta = Parameter('theta', 0.1)
    omega = Parameter('omega', 0.01, lower=0)
    sigma = Parameter('sigma', 1, lower=0)
    params = Parameters([theta, omega, sigma])
    base_model.parameters = params

    eta_name = 'eta'
    eta = NormalDistribution.create(eta_name, 'iiv', 0, omega.symbol)
    sigma_name = 'epsilon'
    sigma = NormalDistribution.create(sigma_name, 'ruv', 0, sigma.symbol)
    rvs = RandomVariables.create([eta, sigma])
    base_model.random_variables = rvs

    y = Assignment(
        sympy.Symbol('Y'), theta.symbol + sympy.Symbol(eta_name) + sympy.Symbol(sigma_name)
    )
    statements = Statements([y])
    base_model.statements = statements

    base_model.dependent_variable = y.symbol
    base_model.observation_transformation = y.symbol
    base_model.name = f'base_{current_iteration}'
    base_model.dataset = _create_dataset(input_model)

    est = EstimationStep('foce', interaction=True, maximum_evaluations=9999)
    base_model.estimation_steps = EstimationSteps([est])
    return base_model


def _create_iiv_on_ruv_model(input_model, current_iteration):
    base_model = input_model
    model = base_model.copy()
    set_iiv_on_ruv(model)
    model.name = f'IIV_on_RUV_{current_iteration}'
    return model


def _create_power_model(input_model, current_iteration):
    base_model = input_model
    model = base_model.copy()
    set_power_on_ruv(model, ipred='IPRED', lower_limit=None, zero_protection=True)
    model.name = f'power_{current_iteration}'
    return model


def _create_time_varying_model(input_model, groups, i, current_iteration):
    base_model = input_model
    model = base_model.copy()
    quantile = i / groups
    cutoff = model.dataset['TAD'].quantile(q=quantile)
    set_time_varying_error_model(model, cutoff=cutoff, idv='TAD')
    model.name = f"time_varying{i}_{current_iteration}"
    return model


def _create_combined_model(input_model, current_iteration):
    base_model = input_model
    model = base_model.copy()
    remove_error_model(model)
    sset = model.statements
    s = sset[0]
    ruv_prop = create_symbol(model, 'epsilon_p')
    ruv_add = create_symbol(model, 'epsilon_a')
    ipred = sympy.Symbol('IPRED')
    s = Assignment(s.symbol, s.expression + ruv_prop + ruv_add / ipred)
    model.statements = s + sset[1:]

    prop_name = 'sigma_prop'
    add_population_parameter(model, prop_name, 1, lower=0)
    df = model.dataset.copy()
    df['IPRED'].replace(0, 2.225e-307, inplace=True)
    model.dataset = df
    ipred_min = model.dataset['IPRED'].min()
    sigma_add_init = ipred_min / 2
    add_name = 'sigma_add'
    add_population_parameter(model, add_name, sigma_add_init, lower=0)

    eps_prop = NormalDistribution.create(ruv_prop.name, 'ruv', 0, sympy.Symbol(prop_name))
    eps_add = NormalDistribution.create(ruv_add.name, 'ruv', 0, sympy.Symbol(add_name))
    model.random_variables = model.random_variables + [eps_prop, eps_add]

    model.name = f'combined_{current_iteration}'
    return model


def _create_dataset(input_model):
    input_model = input_model.copy()
    residuals = input_model.modelfit_results.residuals
    cwres = residuals['CWRES'].reset_index(drop=True)
    predictions = input_model.modelfit_results.predictions
    if 'CIPREDI' in predictions:
        ipredcol = 'CIPREDI'
    elif 'IPRED' in predictions:
        ipredcol = 'IPRED'
    else:
        raise ValueError("Need CIPREDI or IPRED")
    ipred = predictions[ipredcol].reset_index(drop=True)
    mdv = get_mdv(input_model)
    mdv = mdv.reset_index(drop=True)
    label_id = input_model.datainfo.id_column.name
    input_id = input_model.dataset[label_id].astype('int64').squeeze().reset_index(drop=True)
    add_time_after_dose(input_model)
    tad_label = input_model.datainfo.descriptorix['time after dose'][0].name
    tad = input_model.dataset[tad_label].squeeze().reset_index(drop=True)
    df = pd.concat([mdv, input_id, tad, ipred], axis=1)
    df = df[df['MDV'] == 0].reset_index(drop=True)
    df = pd.concat([df, cwres], axis=1).rename(columns={'CWRES': 'DV', ipredcol: 'IPRED'})
    return df


def _time_after_dose(model):
    if 'TAD' in model.dataset:
        pass
    else:
        add_time_after_dose(model)
    return model


def _create_best_model(model, res, current_iteration, groups=4, cutoff=3.84):
    if not res.cwres_models.empty and any(res.cwres_models['dofv'] > cutoff):
        model = model.copy()
        update_initial_estimates(model)
        model.name = f'best_ruvsearch_{current_iteration}'
        selected_model_name = f'base_{current_iteration}'
        idx = res.cwres_models['dofv'].idxmax()
        name = idx[0]

        if current_iteration == 1:
            base_description = ''
        else:
            base_description = model.description + '+'
        model.description = base_description + name

        if name.startswith('power'):
            set_power_on_ruv(model)
            set_initial_estimates(
                model,
                {
                    'power1': res.cwres_models['parameters']
                    .loc['power', 1, current_iteration]
                    .get('theta')
                    + 1
                },
            )
        elif name.startswith('IIV_on_RUV'):
            set_iiv_on_ruv(model)
            set_initial_estimates(
                model,
                {
                    'IIV_RUV1': res.cwres_models['parameters']
                    .loc['IIV_on_RUV', 1, current_iteration]
                    .get('omega')
                },
            )
        elif name.startswith('time_varying'):
            _time_after_dose(model)
            i = int(name[-1])
            quantile = i / groups
            df = _create_dataset(model)
            tad = df['TAD']
            cutoff_tvar = tad.quantile(q=quantile)
            set_time_varying_error_model(model, cutoff=cutoff_tvar, idv='TAD')
            set_initial_estimates(
                model,
                {
                    'time_varying': res.cwres_models['parameters']
                    .loc[f"time_varying{i}", 1, current_iteration]
                    .get('theta')
                },
            )
        else:
            set_combined_error_model(model)
            set_initial_estimates(
                model,
                {
                    'sigma_prop': res.cwres_models['parameters']
                    .loc['combined', 1, current_iteration]
                    .get('sigma_prop'),
                    'sigma_add': res.cwres_models['parameters']
                    .loc['combined', 1, current_iteration]
                    .get('sigma_add'),
                },
            )

        selected_model_name = name
        model.update_source()
    else:
        model = None
        selected_model_name = None
    return model, selected_model_name


@runtime_type_check
@same_arguments_as(create_workflow)
def validate_input(model, groups, p_value, skip):
    if groups <= 0:
        raise ValueError(f'Invalid `groups`: got `{groups}`, must be >= 1.')

    if not 0 < p_value <= 1:
        raise ValueError(f'Invalid `p_value`: got `{p_value}`, must be a float in range (0, 1].')

    if skip is not None and not set(skip).issubset(SKIP):
        raise ValueError(f'Invalid `skip`: got `{skip}`, must be None/NULL or a subset of {SKIP}.')

    if model is not None:

        if model.modelfit_results is None:
            raise ValueError(f'Invalid `model`: {model} is missing modelfit results.')

        residuals = model.modelfit_results.residuals
        if residuals is None or 'CWRES' not in residuals:
            raise ValueError(
                f'Invalid `model`: please check {model.name}.mod file to'
                f' make sure ID, TIME, CWRES are in $TABLE.'
            )

        predictions = model.modelfit_results.predictions
        if predictions is None or ('CIPREDI' not in predictions and 'IPRED' not in predictions):
            raise ValueError(
                f'Invalid `model`: please check {model.name}.mod file to'
                f' make sure ID, TIME, CIPREDI (or IPRED) are in $TABLE.'
            )
