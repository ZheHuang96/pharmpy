from dataclasses import dataclass, field, replace
from functools import partial
from itertools import chain, count
from typing import Literal, Optional, Union

import statsmodels.api as sm

import pharmpy.tools.covsearch.tool as scm_tool
from pharmpy.deps import numpy as np
from pharmpy.deps import pandas as pd
from pharmpy.deps.scipy import stats
from pharmpy.model import Model
from pharmpy.modeling import (
    add_estimation_step,
    calculate_bic,
    get_parameter_rv,
    mu_reference_model,
    remove_covariate_effect,
    remove_estimation_step,
)
from pharmpy.modeling.expressions import depends_on
from pharmpy.modeling.lrt import best_of_two as lrt_best_of_two
from pharmpy.tools.common import (
    create_plots,
    summarize_tool,
    table_final_eta_shrinkage,
    update_initial_estimates,
)
from pharmpy.tools.covsearch.results import COVSearchResults
from pharmpy.tools.covsearch.util import (
    Candidate,
    DummyEffect,
    ForwardStep,
    SearchState,
    StateAndEffect,
    store_input_model,
)
from pharmpy.tools.mfl.feature.covariate import parse_spec, spec
from pharmpy.tools.mfl.helpers import all_funcs
from pharmpy.tools.mfl.parse import ModelFeatures, get_model_features
from pharmpy.tools.modelfit import create_fit_workflow
from pharmpy.tools.run import summarize_errors_from_entries, summarize_modelfit_results_from_entries
from pharmpy.tools.scm.results import candidate_summary_dataframe, ofv_summary_dataframe
from pharmpy.workflows import ModelEntry, Task, Workflow, WorkflowBuilder
from pharmpy.workflows.results import ModelfitResults


@dataclass
class LCSSearchState(SearchState):
    lcs_selection: list = field(default_factory=list)

    def __eq__(self, other):
        if not isinstance(other, SearchState):
            return NotImplemented
        # compare only best_candidate_so_far and all_candidates_so_far
        return (self.best_candidate_so_far, self.all_candidates_so_far) == (
            other.best_candidate_so_far,
            other.all_candidates_so_far,
        )


@dataclass(frozen=True)
class SAMBAStep(ForwardStep):
    pass


@dataclass
class LCSRecord:
    parameter: str
    inclusion: Optional[tuple[str, ...]]  # covariates included in the model
    bic: float
    ofv: float
    dofv: Optional[float]
    lrt_pval: Optional[float]
    estimates: dict[str, float]


@dataclass
class SWLCSRecord(LCSRecord):
    lcs_step: int
    parent: Optional[tuple[str, ...]]  # parent model's inclusion
    selection: list[str]


NAME_WF = 'covsearch'


def samba_workflow(
    search_space: Union[str, ModelFeatures],
    max_steps: int = -1,
    p_forward: float = 0.05,
    p_backward: float = 0.01,
    model: Optional[Model] = None,
    results: Optional[ModelfitResults] = None,
    max_eval: bool = False,
    algorithm: Literal['samba', 'samba-foce', 'scm-lcs'] = 'samba',
    nsamples: int = 10,
    max_covariates: Optional[int] = 3,
    selection_criterion: Literal['bic', 'lrt'] = 'bic',
    linreg_method: Literal['ols', 'wls', 'lme'] = 'ols',
    stepwise_lcs: Optional[bool] = None,
    strictness: Optional[str] = "minimization_successful or (rounding_errors and sigdigs>=0.1)",
):
    """
    Workflow builder for SAMBA covariate search algorithm.
    """

    wb = WorkflowBuilder(name=NAME_WF)

    # Initiate model and search state
    store_task = Task("store_input_model", store_input_model, model, results, max_eval)
    wb.add_task(store_task)

    init_task = Task("init", samba_init_search_state, search_space, nsamples, algorithm)
    wb.add_task(init_task, predecessors=store_task)

    # SAMBA search task
    samba_search_task = Task(
        "samba_search",
        samba_forward,
        nsamples,
        max_steps,
        max_covariates,
        selection_criterion,
        p_forward,
        linreg_method,
        algorithm,
        stepwise_lcs,
    )
    wb.add_task(samba_search_task, predecessors=init_task)
    search_output = wb.output_tasks

    # Results task
    results_task = Task(
        "results",
        samba_task_results,
        p_forward,
        p_backward,
        strictness,
        selection_criterion,
        algorithm,
    )
    wb.add_task(results_task, predecessors=search_output)

    return Workflow(wb)


# ========== SAMBA FORWARD SEARCH ==================
def samba_forward(
    context,
    nsamples,
    max_steps,
    max_covariates,
    selection_criterion,
    lrt_alpha,
    linreg_method,
    algorithm,
    stepwise_lcs,
    state_and_effect,
):
    if algorithm == "scm-lcs" and linreg_method in ["ols", "wls"] and nsamples != 1:
        context.log_warning(
            f"SCM-LCS-{linreg_method.upper()}: nsamples should be 1. Overriding nsamples to 1."
        )
        nsamples = 1
    if algorithm == "scm-lcs" and linreg_method == "lme" and nsamples <= 1:
        raise ValueError("SCM-LCS-LME: nsamples should be greater than 1.")

    if (algorithm.startswith("samba")) and (stepwise_lcs is None):
        stepwise_lcs = True
    if (algorithm.startswith("scm")) and (stepwise_lcs is None):
        stepwise_lcs = False

    search_state = state_and_effect.search_state
    ini_effect_funcs = state_and_effect.effect_funcs

    steps = range(1, max_steps + 1) if max_steps >= 1 else count(1)
    for step in steps:
        # linear screening
        state_and_effect = linear_covariate_selection(
            context,
            step,
            state_and_effect,
            nsamples,
            algorithm,
            max_covariates,
            selection_criterion,
            lrt_alpha,
            linreg_method,
            stepwise_lcs,
        )
        # nonlinear selection
        if algorithm.startswith('samba') or stepwise_lcs:
            search_state = samba_nonlinear_model_selection(
                context, step, selection_criterion, lrt_alpha, state_and_effect
            )
        else:  # algorithm == 'scm-lcs'
            search_state = scmlcs_nonlinear_model_selection(
                context, step, selection_criterion, lrt_alpha, state_and_effect
            )
        if search_state is state_and_effect.search_state:
            break
        else:
            state_and_effect = replace(
                state_and_effect, effect_funcs=ini_effect_funcs, search_state=search_state
            )

    return search_state


# ========== INIT SEARCH STATE =====================
# NOTE: so far the maximum suppported nsamples for samba is 10,
# limited by unable to set $SIZES ISAMPLEMAX=250 automatically with pharmpy
def samba_init_search_state(context, search_space, nsamples, algorithm, input_modelentry):
    model = input_modelentry.model
    effect_funcs, filtered_model = samba_filter_search_space_and_model(search_space, model)
    search_state = samba_init_nonlinear_search_state(
        context, input_modelentry, filtered_model, nsamples, algorithm
    )
    return StateAndEffect(search_state=search_state, effect_funcs=effect_funcs)


def samba_filter_search_space_and_model(search_space, model):
    filtered_model = model.replace(name="filtered_input_model")
    if isinstance(search_space, str):
        search_space = ModelFeatures.create_from_mfl_string(search_space)
    ss_mfl = search_space.expand(filtered_model)
    model_mfl = ModelFeatures.create_from_mfl_string(get_model_features(filtered_model))

    covariate_to_keep = model_mfl - ss_mfl
    covariate_to_remove = model_mfl - covariate_to_keep
    covariate_to_remove = covariate_to_remove.mfl_statement_list(["covariate"])
    description = ["REMOVE"]
    if len(covariate_to_remove) != 0:
        for cov_effect in parse_spec(spec(filtered_model, covariate_to_remove)):
            filtered_model = remove_covariate_effect(filtered_model, cov_effect[0], cov_effect[1])
            description.append(f'({cov_effect[0]}-{cov_effect[1]}-{cov_effect[2]})')

    covariate_to_keep = covariate_to_keep.mfl_statement_list(["covariate"])
    for cov_effect in parse_spec(spec(filtered_model, covariate_to_keep)):
        if cov_effect[2].lower == "custom":
            filtered_model = remove_covariate_effect(filtered_model, cov_effect[0], cov_effect[1])
            description.append(f'({cov_effect[0]}-{cov_effect[1]}-{cov_effect[2]})')

    structural_cov = tuple(c for c in ss_mfl.covariate if not c.optional.option)
    structural_cov_funcs = all_funcs(Model(), structural_cov)
    if len(structural_cov_funcs) != 0:
        description.append("ADD_STRUCT")
        for cov_effect, cov_func in structural_cov_funcs.items():
            filtered_model = cov_func(filtered_model)
            description.append(f'({cov_effect[0]}-{cov_effect[1]}-{cov_effect[2]})')
    description.append("ADD_EXPLOR")
    filtered_model = filtered_model.replace(description="input;" + ";".join(description))

    exploratory_cov = tuple(c for c in ss_mfl.covariate if c.optional.option)
    exploratory_cov_funcs = all_funcs(Model(), exploratory_cov)
    exploratory_cov_funcs = {
        cov_effect[1:-1]: cov_func
        for cov_effect, cov_func in exploratory_cov_funcs.items()
        if cov_effect[-1] == "ADD"
    }
    return (exploratory_cov_funcs, filtered_model)


def samba_init_nonlinear_search_state(
    context, input_modelentry, filtered_model, nsamples, algorithm
):
    if algorithm.startswith("samba"):
        filtered_model = set_samba_estimation(filtered_model, nsamples, algorithm)
    else:
        filtered_model = set_scmlcs_estimation(filtered_model)

    if filtered_model != input_modelentry.model or input_modelentry.modelfit_results is None:
        filtered_modelentry = ModelEntry.create(model=filtered_model)
        fit_workflow = create_fit_workflow(modelentries=[filtered_modelentry])
        filtered_modelentry = context.call_workflow(fit_workflow, "fit_filtered_model")
    else:
        filtered_modelentry = input_modelentry

    candidate = Candidate(modelentry=filtered_modelentry, steps=())
    return LCSSearchState(input_modelentry, filtered_modelentry, candidate, [candidate])


def set_samba_estimation(model, nsamples, algorithm):
    model = mu_reference_model(model)
    model = remove_estimation_step(model, idx=0)
    model = add_estimation_step(
        model,
        method="ITS",
        idx=0,
        interaction=True,
        auto=True,
        niter=5,
    )

    if algorithm == "samba-foce":
        model = add_estimation_step(
            model,
            method="FOCE",
            idx=1,
            interaction=True,
            auto=True,
            tool_options={"NOABORT": 0, "PHITYPE": "1", "FNLETA": "0"},
        )
    else:
        model = add_estimation_step(
            model,
            method="SAEM",
            idx=1,
            interaction=True,
            auto=True,
            niter=200,
            isample=10,
            keep_every_nth_iter=50,
            tool_options={"PHITYPE": "1", "FNLETA": "0"},
        )

    model = add_estimation_step(
        model,
        method="SAEM",
        idx=2,
        niter=0,
        isample=nsamples,
        tool_options={"EONLY": "1", "NBURN": "0", "MASSRESET": "0", "ETASAMPLES": "1"},
    )
    model = add_estimation_step(
        model,
        method="IMP",
        idx=3,
        auto=True,
        niter=20,
        isample=1000,
        tool_options={"EONLY": "1", "MASSRESET": "1", "ETASAMPLES": "0"},
    )
    return model


def set_scmlcs_estimation(model):
    if model.execution_steps[0].method != "FOCE":
        model = remove_estimation_step(model, idx=0)
        model = add_estimation_step(
            model,
            method="FOCE",
            idx=0,
            interaction=True,
            auto=True,
            maximum_evaluations=9999,
            tool_options={"PHITYPE": "1", "FNLETA": "0"},
        )
    return model


# ============= LINEAR COVARIATE SCREENING =================
def linear_covariate_selection(
    context,
    step,
    state_and_effect,
    nsamples,
    algorithm,
    max_covariates=2,
    selection_criterion="bic",
    lrt_alpha=0.05,
    linreg_method="ols",
    stepwise_lcs=None,
):
    search_state = state_and_effect.search_state
    effect_funcs = state_and_effect.effect_funcs
    modelentry = search_state.best_candidate_so_far.modelentry

    param_indexed_covars = _coveffect_key2list(effect_funcs)
    params = list(param_indexed_covars.keys())
    covars = list(set(chain(*param_indexed_covars.values())))
    data = create_linear_covariate_dataset(
        modelentry, params, covars, algorithm, linreg_method, nsamples
    )
    selected_covariates = {}
    selection_results = pd.DataFrame({})

    for param, covs in param_indexed_covars.items():
        eta_name = get_parameter_rv(modelentry.model, param, "iiv")[0]
        if stepwise_lcs:
            selected, model_table = _stepwise_linear_covariate_selection(
                data,
                eta_name,
                covs,
                nsamples=nsamples,
                max_covariates=max_covariates,
                selection_criterion=selection_criterion,
                lrt_alpha=lrt_alpha,
                linreg_method=linreg_method,
            )
        else:
            selected, model_table = _linear_covariate_selection(
                data,
                eta_name,
                covs,
                nsamples=nsamples,
                max_covariates=max_covariates,
                selection_criterion=selection_criterion,
                lrt_alpha=lrt_alpha,
                linreg_method=linreg_method,
            )
        model_table["parameter"] = param

        selected_covariates[param] = selected
        selection_results = pd.concat([selection_results, model_table], ignore_index=True)

    selection_results.insert(0, "covsearch_step", step)
    search_state.lcs_selection.append(selection_results)

    coveffect_keys = _coveffect_list2key(selected_covariates)
    coveffect_funcs = _retrieve_covfunc(effect_funcs, coveffect_keys)

    if stepwise_lcs:
        context.log_info(
            f"STEP {step} | STEPWISE LINEAR SELECTION\n"
            f"    Selected Covariate Effects: {list(coveffect_funcs.keys())}"
        )
    else:
        context.log_info(
            f"STEP {step} | LINEAR COVARIATE SCREENING\n"
            f"    Reduced Search Space: {list(coveffect_funcs.keys())}"
        )

    return StateAndEffect(effect_funcs=coveffect_funcs, search_state=search_state)


def create_linear_covariate_dataset(
    modelentry, parameters, covariates, algorithm, linreg_method, nsamples
):
    # ensure `parameters` is a list
    parameters = list(parameters) if not isinstance(parameters, list) else parameters

    # retrieve ETA or ETA samples for specified parameters
    etas = [get_parameter_rv(modelentry.model, param)[0] for param in parameters]
    if algorithm.startswith("samba"):
        eta_columns = modelentry.modelfit_results.individual_eta_samples[etas]
    elif algorithm.startswith("scm") and linreg_method == "lme":
        eta_columns = _sample_eta_multivariate_normal(modelentry, nsamples)[etas]
    else:
        eta_columns = modelentry.modelfit_results.individual_estimates[etas]

    # separate and process covariates
    log_covars = [covar for covar in covariates if covar.startswith("log")]
    covars2trans = [covar.replace("log", "") for covar in log_covars]
    covars = set([covar for covar in covariates if not covar.startswith("log")] + covars2trans)

    # extract covariate data
    covariate_columns = modelentry.model.dataset[["ID"] + list(covars)].drop_duplicates()

    # apply log transformation to applicable covariates
    covariate_columns.loc[:, log_covars] = (
        covariate_columns.loc[:, covars2trans].apply(np.log).values
    )
    # merge ETA columns with covariates
    dataset = covariate_columns.merge(eta_columns, on="ID")

    # add ETC values
    for eta in etas:
        etc = [
            covarmatrix.loc[eta, eta].squeeze()
            for covarmatrix in modelentry.modelfit_results.individual_estimates_covariance
        ]
        subject_id = modelentry.modelfit_results.individual_estimates.index
        etc_column = pd.DataFrame({"ID": subject_id, eta.replace("ETA", "ETC"): etc})
        dataset = dataset.merge(etc_column, on="ID")

    return dataset


def _sample_eta_multivariate_normal(modelentry, nsamples):
    eta_names = modelentry.modelfit_results.individual_estimates.columns
    subject_id = modelentry.modelfit_results.individual_estimates.index
    nsubjects = len(subject_id.unique())
    subject_id = np.repeat(subject_id, nsamples)
    samples = np.empty((nsubjects * nsamples, len(eta_names)))

    idx = 0
    for mu, covmat in zip(
        modelentry.modelfit_results.individual_estimates.values,
        modelentry.modelfit_results.individual_estimates_covariance,
    ):
        eta = np.random.multivariate_normal(mu, covmat, nsamples)
        samples[idx : idx + nsamples] = eta
        idx += nsamples

    eta_columns = pd.DataFrame(
        np.column_stack((subject_id, samples)), columns=["ID"] + eta_names.to_list()
    )
    eta_columns = eta_columns.set_index("ID")

    return eta_columns


def _stepwise_linear_covariate_selection(
    data,
    parameter,
    covariates,
    nsamples,
    max_covariates=2,
    selection_criterion="bic",
    lrt_alpha=0.05,
    linreg_method="ols",
):
    """
    Perform linear stepwsie covariate screening using BIC or LRT
    """
    lin_func = _get_linreg_method(linreg_method, data, parameter)

    partial_ofv = partial(_ofv, nsamples=nsamples, linreg_method=linreg_method)
    partial_bic = partial(_bic, nsamples=nsamples, linreg_method=linreg_method)
    partial_lrt = partial(_lrt, nsamples=nsamples, linreg_method=linreg_method)

    lin_step = 0
    selected = []
    remaining = covariates
    model_records = []  # list to store evaluation details

    # prepare the data
    data_with_const = sm.add_constant(data)
    y = data_with_const[parameter]

    # fit the base model (no covariates)
    X_base = data_with_const[["const"]]
    base_model = lin_func(endog=y, exog=X_base).fit()
    base_bic = partial_bic(base_model)
    base_ofv = partial_ofv(base_model)
    best_model = base_model
    best_bic = base_bic

    # record base model details
    base_record = SWLCSRecord(
        lcs_step=1,
        parameter=parameter,
        inclusion=None,
        bic=base_bic,
        ofv=base_ofv,
        dofv=None,
        lrt_pval=None,
        parent=None,
        selection=selected,
        estimates=base_model.params.to_dict(),
    )
    model_records.append(base_record)

    # linear stepwise covariate screening
    while remaining and (max_covariates is None or len(selected) < max_covariates):
        scores, models = {}, {}

        for covariate in remaining:
            # TODO: parallel the linear model fitting runs
            X = data_with_const[["const"] + selected + [covariate]]
            # NOTE: an issue specific to statsmodels: we would like to use df_model to get number of parameteres
            # however, the df_model is defined as the rank of the regressor matrix MINUS ONE if a constant is included
            # either use hasconst=False or change the samba_bic to df_model+1
            model = lin_func(endog=y, exog=X).fit()
            model_bic = partial_bic(model)
            model_ofv = partial_ofv(model)
            lrt_dofv, lrt_pval = partial_lrt(best_model, model)

            scores[covariate] = model_bic if selection_criterion == "bic" else lrt_pval
            models[covariate] = model

            # store model evaluation details
            model_record = SWLCSRecord(
                lcs_step=lin_step + 1,
                parameter=parameter,
                inclusion=tuple(selected + [covariate]),
                bic=model_bic,
                ofv=model_ofv,
                dofv=lrt_dofv,
                lrt_pval=lrt_pval,
                parent=tuple(selected),
                selection=selected,
                estimates=model.params.to_dict(),
            )
            model_records.append(model_record)

        # select the best candidate
        best_candidate = min(scores, key=scores.get)
        if (selection_criterion == "bic" and scores[best_candidate] < best_bic) or (
            selection_criterion == "lrt" and scores[best_candidate] < lrt_alpha
        ):
            selected.append(best_candidate)
            remaining.remove(best_candidate)
            best_model = models[best_candidate]
            best_bic = partial_bic(best_model)

            lin_step += 1
        # stop if no improvement is made
        else:
            break

    # covert records to DataFrame
    lcsres_table = pd.DataFrame([record.__dict__ for record in model_records])
    lcsres_table = lcsres_table.sort_values(
        ["lcs_step", "lrt_pval"] if selection_criterion == "lrt" else "bic"
    )

    return selected, lcsres_table


def _linear_covariate_selection(
    data,
    parameter,
    covariates,
    nsamples=1,
    max_covariates=None,
    selection_criterion="lrt",
    lrt_alpha=0.05,
    linreg_method="ols",
):
    """Linear covariate selection using BIC or LRT"""
    lin_func = _get_linreg_method(linreg_method, data, parameter)

    partial_ofv = partial(_ofv, nsamples=nsamples, linreg_method=linreg_method)
    partial_bic = partial(_bic, nsamples=nsamples, linreg_method=linreg_method)
    partial_lrt = partial(_lrt, nsamples=nsamples, linreg_method=linreg_method)

    # prepare the data and initialize info holders
    data_with_const = sm.add_constant(data)
    y = data[parameter]
    model_records = []
    scores = {}

    # fit the base model
    X_base = data_with_const[["const"]]
    base_model = lin_func(endog=y, exog=X_base).fit()
    base_bic = partial_bic(base_model)
    base_ofv = partial_ofv(base_model)

    # record base model
    base_res = LCSRecord(
        parameter=parameter,
        inclusion=None,
        bic=base_bic,
        ofv=base_ofv,
        dofv=None,
        lrt_pval=None,
        estimates=base_model.params.to_dict(),
    )
    model_records.append(base_res)

    for covariate in covariates:
        # TODO: parallel the linear model fitting runs
        X = data_with_const[["const", covariate]]
        model = lin_func(endog=y, exog=X).fit()
        model_bic = partial_bic(model)
        model_ofv = partial_ofv(model)
        lrt_dofv, lrt_pval = partial_lrt(base_model, model)

        scores[covariate] = model_bic if selection_criterion == "bic" else lrt_pval
        model_res = LCSRecord(
            parameter=parameter,
            inclusion=tuple([covariate]),
            bic=model_bic,
            ofv=model_ofv,
            dofv=lrt_dofv,
            lrt_pval=lrt_pval,
            estimates=model.params.to_dict(),
        )
        model_records.append(model_res)

    sorted_candidates = sorted(scores.items(), key=lambda x: x[1])
    max_covariates = min(max_covariates or len(sorted_candidates), len(sorted_candidates))
    selected = [
        candidate
        for candidate, score in sorted_candidates[:max_covariates]
        if (selection_criterion == "bic" and score < base_bic)
        or (selection_criterion == "lrt" and score < lrt_alpha)
    ]  # it would return all test positive candidates if max_covariates is None

    lcsres_table = pd.DataFrame([record.__dict__ for record in model_records])
    lcsres_table = lcsres_table.sort_values("lrt_pval" if selection_criterion == "lrt" else "bic")
    lcsres_table["lrt_positive"] = np.where(lcsres_table["lrt_pval"] < lrt_alpha, True, False)

    return selected, lcsres_table


def _get_linreg_method(linreg_method, data, parameter):
    lin_func_map = {
        "ols": partial(sm.OLS),
        "wls": partial(sm.WLS, weights=1.0 / data[parameter.replace("ETA", "ETC")]),
        "lme": partial(sm.MixedLM, groups=data["ID"]),
    }
    lin_func = lin_func_map.get(linreg_method)
    if not lin_func:
        raise ValueError(f"Unsupported regression method: {linreg_method}")
    return lin_func


def _ofv(modelfit, nsamples, linreg_method="ols"):
    """Calculate OFV for different linear regression methods."""

    if linreg_method not in ["ols", "wls", "lme"]:
        raise ValueError(f"Unsupported regression method: {linreg_method}")

    scaling = 1 if linreg_method == "lme" else 1 / nsamples
    ofv = -2 * modelfit.llf * scaling

    return ofv


def _bic(modelfit, nsamples, linreg_method):
    """Calculate BICc for different linear regression methods."""

    df = modelfit.df_modelwc if linreg_method == "lme" else modelfit.df_model + 1
    bic = _ofv(modelfit, nsamples, linreg_method) + np.log(modelfit.nobs / nsamples) * df

    return bic


def _lrt(parent, child, nsamples, linreg_method):
    """Perform LRT for different linear regression methods."""

    df_key = "df_modelwc" if linreg_method == "lme" else "df_model"
    lrt_dofv = _ofv(parent, nsamples, linreg_method) - _ofv(child, nsamples, linreg_method)
    lrt_df = getattr(child, df_key) - getattr(parent, df_key)
    lrt_pval = stats.chi2.sf(lrt_dofv, lrt_df)

    return lrt_dofv, lrt_pval


def _coveffect_key2list(effect_funcs):  # coveffect_key2list
    coveffect_list = {}

    for key in effect_funcs.keys():
        # initialize the list
        if key[0] not in coveffect_list:
            coveffect_list[key[0]] = []

        if key[2] == "pow":
            coveffect_list[key[0]].append("log" + key[1])
        else:
            coveffect_list[key[0]].append(key[1])
    return coveffect_list


def _coveffect_list2key(coveffect_list):
    coveffect_keys = []
    for param, covars in coveffect_list.items():
        coveffect_keys.extend([(param, covar) for covar in covars])
    return coveffect_keys


def _retrieve_covfunc(effect_funcs, coveffect_keys):
    retrieved_funcs = dict()
    for item in coveffect_keys:
        if item[1].startswith("log"):
            param, covar = item
            covar = covar.replace("log", "")
            key = (param, covar, "pow", "*")
            retrieved_funcs |= {key: effect_funcs[key]}
        else:
            for shape in ["exp", "cat", "lin"]:
                param, covar = item
                key = (param, covar, shape, "*")
                if key in effect_funcs:
                    retrieved_funcs |= {key: effect_funcs[key]}
    return retrieved_funcs


# ============ NONLINEAR MODEL SELECTION =================
def samba_nonlinear_model_selection(
    context, step, selection_criterion, lrt_alpha, state_and_effect
):
    # unpack state_and_effect
    search_state, effect_funcs = state_and_effect.search_state, state_and_effect.effect_funcs
    best_candidate = search_state.best_candidate_so_far
    best_model = best_candidate.modelentry.model
    best_bic = calculate_bic(
        best_model, best_candidate.modelentry.modelfit_results.ofv, type="mixed"
    )
    best_ofv = best_candidate.modelentry.modelfit_results.ofv

    # early exit if no effects
    if not effect_funcs:
        context.log_info(
            f"STEP {step} | NONLINEAR MODEL SELECTION\n"
            f"    No covariate effects found from linear covariate screening"
        )
        return search_state

    # prepare the new nonlinear model
    updated_model = update_initial_estimates(best_model, best_candidate.modelentry.modelfit_results)
    updated_desc = best_model.description
    updated_steps = best_candidate.steps
    update_occurs = False  # Track whether any update occurs to the model

    # add covariate effects to nonlinear model
    for cov_effect, cov_func in effect_funcs.items():
        # check if covariate effect already exists
        if depends_on(updated_model, cov_effect[0], cov_effect[1]):
            context.log_info(
                f"STEP {step} | NONLINEAR MODEL SELECTION\n"
                f"    Covariate effect of {cov_effect[1]} on {cov_effect[0]} already exists"
            )
            continue

        updated_desc = updated_desc + f";({'-'.join(cov_effect[:3])})"
        updated_model = cov_func(updated_model)
        updated_steps = updated_steps + (SAMBAStep(lrt_alpha, DummyEffect(*cov_effect)),)
        update_occurs = True

    # if no changes are made, skip further processing
    if not update_occurs:
        context.log_info(
            f"STEP {step} | NONLINEAR MODEL SELECTION\n" f"    No new covariate effects are added."
        )
        return search_state

    updated_model = updated_model.replace(name=f"sabma_step{step}", description=updated_desc)

    # fit the updated model
    updated_modelentry = ModelEntry.create(model=updated_model, parent=best_model)
    fit_workflow = create_fit_workflow(modelentries=[updated_modelentry])
    updated_modelentry = context.call_workflow(fit_workflow, "fit_updated_model")
    updated_modelfit = updated_modelentry.modelfit_results
    updated_model_bic = calculate_bic(updated_model, updated_modelfit.ofv, type="mixed")
    updated_model_ofv = updated_modelfit.ofv

    lrt_best_modelentry = lrt_best_of_two(
        best_candidate.modelentry, updated_modelentry, best_ofv, updated_model_ofv, lrt_alpha
    )

    # add the new candidate to the search state
    new_candidate = Candidate(updated_modelentry, steps=updated_steps)
    search_state.all_candidates_so_far.append(new_candidate)

    # update the best candidate if the new model is better
    context.log_info(
        f"STEP {step} | NONLINEAR MODEL SELECTION\n"
        f"    Best Model So Far: BIC {best_bic:.2f} | OFV {best_ofv:.2f}\n"
        f"    Updated Model    : BIC {updated_model_bic:.2f} | OFV {updated_model_ofv:.2f} | "
        f"dOFV {best_ofv - updated_model_ofv:.2f}"
    )
    if (selection_criterion == "bic" and updated_model_bic < best_bic) or (
        selection_criterion == "lrt" and lrt_best_modelentry == updated_modelentry
    ):
        search_state = replace(search_state, best_candidate_so_far=new_candidate)

    return search_state


def scmlcs_nonlinear_model_selection(
    context, step, selection_criterion, lrt_alpha, state_and_effect
):
    """Perform nonlinear model selection for SCM-LCS using either BIC or LRT criteria"""
    # unpack state_and_effect
    search_state = state_and_effect.search_state
    effect_funcs = state_and_effect.effect_funcs
    best_candidate = search_state.best_candidate_so_far
    best_model = best_candidate.modelentry.model
    best_ofv = best_candidate.modelentry.modelfit_results.ofv
    best_bic = calculate_bic(best_model, best_ofv, type="mixed")

    # early exit if no effects
    if not effect_funcs:
        context.log_info(
            f"STEP {step} | NONLINEAR MODEL SELECTION\n"
            f"    No covariate effects found from linear covariate screening"
        )
        return search_state

    # prepare new nonlinear models
    updated_model = update_initial_estimates(best_model, best_candidate.modelentry.modelfit_results)
    scores, new_models, candidate_steps = {}, {}, {}
    for cov_effect, cov_func in effect_funcs.items():

        if depends_on(updated_model, cov_effect[0], cov_effect[1]):
            context.log_info(
                f"STEP {step} | NONLINEAR MODEL SELECTION\n"
                f"    Covariate effect of {cov_effect[1]} on {cov_effect[0]} already exists"
            )
            continue

        name = f"step{step}_" + "_".join(cov_effect[:3])
        desc = best_model.description + f";({'-'.join(cov_effect[:3])})"
        model = updated_model.replace(name=name, description=desc)
        model = cov_func(model)
        new_models[cov_effect] = model
        candidate_steps[cov_effect] = best_candidate.steps + (
            ForwardStep(lrt_alpha, DummyEffect(*cov_effect)),
        )

    new_modelentries = [
        ModelEntry.create(model=model, parent=best_model) for model in new_models.values()
    ]
    fit_wf = create_fit_workflow(modelentries=new_modelentries)  # TODO: try fit with dictionary
    wb = WorkflowBuilder(fit_wf)
    task_gather = Task("gather", lambda *models: models)
    wb.add_task(task_gather, predecessors=wb.output_tasks)
    new_modelentries = context.call_workflow(Workflow(wb), "fit_nonlinear_models")
    model_map = {me.model: me for me in new_modelentries}
    new_mes = {
        cov_effect: model_map[model]
        for cov_effect, model in new_models.items()
        if model in model_map
    }

    scores = {
        cov_effect: (
            _nonlinear_step_lrt(best_candidate.modelentry, me)
            if selection_criterion == "lrt"
            else calculate_bic(me.model, me.modelfit_results.ofv, "mixed")
        )
        for cov_effect, me in new_mes.items()
    }

    new_candidates = {
        cov_effect: Candidate(me, candidate_steps[cov_effect]) for cov_effect, me in new_mes.items()
    }
    search_state.all_candidates_so_far.extend(new_candidates.values())

    best_candidate_key = min(scores, key=scores.get)
    if (selection_criterion == "bic" and scores[best_candidate_key] < best_bic) or (
        selection_criterion == "lrt" and scores[best_candidate_key] < lrt_alpha
    ):
        search_state = replace(
            search_state, best_candidate_so_far=new_candidates[best_candidate_key]
        )
        best_candidate_model = new_mes[best_candidate_key].model
        best_candidate_modelfit = new_mes[best_candidate_key].modelfit_results
        best_candidate_ofv = best_candidate_modelfit.ofv
        best_candidate_bic = calculate_bic(best_candidate_model, best_candidate_ofv, "mixed")

        context.log_info(
            f"STEP {step} | NONLINEAR MODEL SELECTION\n"
            f"    Best Model So Far: BIC {best_bic:.2f} | OFV {best_ofv:.2f}\n"
            f"    Best Candidate {best_candidate_key}: BIC {best_candidate_bic:.2f} | OFV {best_candidate_ofv:.2f}\n"
            f"    dOFV: {best_ofv - best_candidate_ofv:.2f}"
        )
    return search_state


def _nonlinear_step_lrt(parent, child):
    df = len(child.model.parameters) - len(parent.model.parameters)
    lrt_dofv = parent.modelfit_results.ofv - child.modelfit_results.ofv
    lrt_pval = stats.chi2.sf(lrt_dofv, df)
    return lrt_pval


# ============ Results =================
def samba_task_results(
    context,
    p_forward: float,
    p_backward: float,
    strictness: str | None,
    selection_criterion: str,
    algorithm: str,
    state: LCSSearchState,
):
    candidates = state.all_candidates_so_far
    modelentries = list(map(lambda candidate: candidate.modelentry, candidates))
    base_modelentry, *rest_modelentries = modelentries
    assert base_modelentry is state.start_modelentry
    best_modelentry = state.best_candidate_so_far.modelentry
    user_input_modelentry = state.user_input_modelentry
    lcs_results = _make_lcs_table(state.lcs_selection, p_forward)
    tables = _samba_create_result_tables(
        candidates,
        best_modelentry,
        user_input_modelentry,
        base_modelentry,
        rest_modelentries,
        lrt_cutoff=(p_forward, p_backward),
        bic_cutoff=10,  # TODO: test this option
        strictness=strictness,
        selection_criterion=selection_criterion,
        algorithm=algorithm,
    )
    plots = create_plots(best_modelentry.model, best_modelentry.modelfit_results)

    res = COVSearchResults(
        final_model=best_modelentry.model,
        final_results=best_modelentry.modelfit_results,
        summary_models=tables["summary_models"],
        summary_tool=tables["summary_tool"],
        summary_errors=tables["summary_errors"],
        final_model_dv_vs_ipred_plot=plots['dv_vs_ipred'],
        final_model_dv_vs_pred_plot=plots['dv_vs_pred'],
        final_model_cwres_vs_idv_plot=plots['cwres_vs_idv'],
        final_model_abs_cwres_vs_ipred_plot=plots['abs_cwres_vs_ipred'],
        final_model_eta_distribution_plot=plots['eta_distribution'],
        final_model_eta_shrinkage=table_final_eta_shrinkage(
            best_modelentry.model, best_modelentry.modelfit_results
        ),
        linear_covariate_screening_summary=lcs_results,
        steps=tables["steps"],
        ofv_summary=tables["ofv_summary"],
        candidate_summary=tables["candidate_summary"],
    )
    context.store_final_model_entry(best_modelentry)
    context.log_info("Finishing tool covsearch")
    return res


def _samba_create_result_tables(
    candidates,
    best_modelentry,
    input_modelentry,
    base_modelentry,
    res_modelentries,
    lrt_cutoff,
    bic_cutoff,
    strictness,
    selection_criterion,
    algorithm,
):
    model_entries = [base_modelentry] + res_modelentries
    if input_modelentry != base_modelentry:
        model_entries.insert(0, input_modelentry)
        sum_tool_lrt = summarize_tool(
            model_entries,
            base_modelentry,
            rank_type="lrt",
            cutoff=lrt_cutoff,
            strictness=strictness,
        )
        sum_tool_lrt["lrt_pval"] = stats.chi2.sf(sum_tool_lrt["dofv"], sum_tool_lrt["d_params"])
        sum_tool_bic = summarize_tool(
            model_entries,
            base_modelentry,
            rank_type="bic",
            cutoff=bic_cutoff,
            strictness=strictness,
            bic_type="mixed",
        )
        if selection_criterion == "bic":  # TODO: 1) mark selection; 2) don't change the row orders
            sum_tool = sum_tool_bic.merge(
                sum_tool_lrt[["dofv", "ofv", "rank"]], on="model", suffixes=("_bic", "_lrt")
            )
        elif selection_criterion == "lrt":
            sum_tool = sum_tool_lrt.merge(
                sum_tool_bic[["dbic", "bic", "rank"]], on="model", suffixes=("_lrt", "_bic")
            )
        else:
            sum_tool = sum_tool_lrt
    sum_models = summarize_modelfit_results_from_entries(model_entries)
    sum_errors = summarize_errors_from_entries(model_entries)

    steps, ofv_summary, candidate_summary = None, None, None
    if algorithm == "scm-lcs":
        steps = scm_tool._make_df_steps(best_modelentry, candidates)
        sum_tool = _modify_summary_tool(sum_tool, steps)
        ofv_summary = ofv_summary_dataframe(steps, final_included=True, iterations=True)
        candidate_summary = candidate_summary_dataframe(steps)

    return {
        "summary_tool": sum_tool,
        "summary_models": sum_models,
        "summary_errors": sum_errors,
        "steps": steps,
        "ofv_summary": ofv_summary,
        "candidate_summary": candidate_summary,
    }


def _make_lcs_table(lcs_results: list[pd.DataFrame], lrt_alpha):
    desired_order = [
        "covsearch_step",
        "lcs_step",
        "parameter",
        "selection",
        "parent",
        "inclusion",
        "lrt_positive",
        "bic",
        "ofv",
        "dofv",
        "goal_pvalue",
        "lrt_pval",
        "estimates",
    ]
    lcs_table = pd.concat(lcs_results, ignore_index=True)
    lcs_table["goal_pvalue"] = lrt_alpha
    reorder = [col for col in desired_order if col in lcs_table.columns]
    lcs_table = lcs_table[reorder]
    return lcs_table


def _modify_summary_tool(summary_tool, steps):
    step_cols_to_keep = ['step', 'pvalue', 'goal_pvalue', 'is_backward', 'selected', 'model']
    steps_df = steps.reset_index()[step_cols_to_keep].set_index(['step', 'model'])

    summary_tool_new = steps_df.join(summary_tool)
    column_to_move = summary_tool_new.pop('description')

    summary_tool_new.insert(0, 'description', column_to_move)

    return summary_tool_new
