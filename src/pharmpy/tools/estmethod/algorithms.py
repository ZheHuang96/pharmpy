import itertools

from pharmpy.modeling import add_estimation_step, remove_estimation_step, set_ode_solver
from pharmpy.tools.common import update_initial_estimates
from pharmpy.tools.modelfit import create_fit_workflow
from pharmpy.workflows import ModelEntry, Task, Workflow, WorkflowBuilder


def exhaustive(methods, solvers, parameter_uncertainty_methods):
    wb = WorkflowBuilder()

    task_start = Task('start', start)
    wb.add_task(task_start)

    add_eval_after_est = len(methods) > 1

    candidate_no = 1
    for method, solver, parameter_uncertainty_method in itertools.product(
        methods, solvers, parameter_uncertainty_methods
    ):
        model_name = f'estmethod_run{candidate_no}'
        task_create_candidate = Task(
            'create_candidate',
            _create_candidate_model,
            model_name,
            method,
            solver,
            parameter_uncertainty_method,
            add_eval_after_est,
            False,
            False,
        )

        wb.add_task(task_create_candidate, predecessors=task_start)
        candidate_no += 1

    return Workflow(wb), None


def exhaustive_with_update(methods, solvers, parameter_uncertainty_methods):
    wb = WorkflowBuilder()

    add_eval_after_est = len(methods) > 1

    parameter_uncertainty_method_base = (
        parameter_uncertainty_methods[0] if 'FOCE' in methods else None
    )

    task_base_model = Task(
        'create_base_model',
        _create_base_model,
        parameter_uncertainty_method_base,
        add_eval_after_est,
    )
    wb.add_task(task_base_model)
    wf_fit = create_fit_workflow(n=1)
    wb.insert_workflow(wf_fit, predecessors=task_base_model)
    task_base_model_fit = wb.output_tasks

    candidate_no = 1
    for method, solver, parameter_uncertainty_method in itertools.product(
        methods, solvers, parameter_uncertainty_methods
    ):
        # This is equivalent to the base model
        if not (
            method == 'FOCE'
            and solver is None
            and parameter_uncertainty_method == parameter_uncertainty_methods[0]
        ):
            model_name = f'estmethod_run{candidate_no}'
            task_create_candidate = Task(
                'create_candidate',
                _create_candidate_model,
                model_name,
                method,
                solver,
                parameter_uncertainty_method,
                add_eval_after_est,
                False,
                False,
            )
            wb.add_task(task_create_candidate, predecessors=task_base_model_fit)
            candidate_no += 1

        # Create model with updated estimates from FOCE
        model_name = f'estmethod_run{candidate_no}'
        task_create_candidate = Task(
            'create_candidate',
            _create_candidate_model,
            model_name,
            method,
            solver,
            parameter_uncertainty_method,
            add_eval_after_est,
            True,
            False,
        )
        wb.add_task(task_create_candidate, predecessors=task_base_model_fit)
        candidate_no += 1

    return Workflow(wb), task_base_model_fit


def exhaustive_only_eval(methods, solvers, parameter_uncertainty_methods):
    wb = WorkflowBuilder()

    task_start = Task('start', start)
    wb.add_task(task_start)

    add_eval_after_est = len(methods) > 1

    candidate_no = 1
    for method, solver, parameter_uncertainty_method in itertools.product(
        methods, solvers, parameter_uncertainty_methods
    ):
        model_name = f'estmethod_run{candidate_no}'
        task_create_candidate = Task(
            'create_candidate',
            _create_candidate_model,
            model_name,
            method,
            solver,
            parameter_uncertainty_method,
            add_eval_after_est,
            False,
            True,
        )

        wb.add_task(task_create_candidate, predecessors=task_start)
        candidate_no += 1

    return Workflow(wb), None


def start(model_entry):
    return model_entry


def _create_base_model(parameter_uncertainty_method, add_eval_after_est, model_entry):
    model = model_entry.model

    est_settings = _create_est_settings("FOCE", parameter_uncertainty_method, add_eval_after_est)
    est_method = est_settings["method"]
    all_methods = [est_method]

    if add_eval_after_est:
        eval_settings = _create_eval_settings(
            laplace=False, parameter_uncertainty_method=parameter_uncertainty_method
        )
        eval_method = eval_settings["method"]
        all_methods.append(eval_method)
    else:
        eval_settings = eval_method = None

    base_model = model.replace(name="base_model")

    base_model = base_model.replace(
        description=_create_description(
            methods=all_methods,
            parameter_uncertainty_method=parameter_uncertainty_method,
            solver=None,
            update_inits=False,
        )
    )

    while len(base_model.estimation_steps) > 0:
        base_model = remove_estimation_step(base_model, 0)

    base_model = add_estimation_step(base_model, **est_settings)
    if add_eval_after_est:
        base_model = add_estimation_step(base_model, **eval_settings)

    base_entry = ModelEntry.create(base_model, modelfit_results=None, parent=model_entry.model)
    return base_entry


def _create_candidate_model(
    model_name,
    method,
    solver,
    parameter_uncertainty_method,
    add_eval_after_est,
    update_inits,
    only_evaluation,
    model_entry,
):
    if update_inits:
        model = update_initial_estimates(model_entry.model, model_entry.modelfit_results)
    else:
        model = model_entry.model

    est_settings = None
    eval_settings = None

    laplace = True if method == 'LAPLACE' else False

    if only_evaluation:
        eval_settings = _create_eval_settings(laplace, parameter_uncertainty_method)
        all_methods = [eval_settings['method']]
    else:
        est_settings = _create_est_settings(
            method, parameter_uncertainty_method, add_eval_after_est
        )
        all_methods = [est_settings['method']]

        if add_eval_after_est:
            eval_settings = _create_eval_settings(laplace, parameter_uncertainty_method)
            all_methods.append(eval_settings['method'])

    model = model.replace(
        name=model_name,
        description=_create_description(
            methods=all_methods,
            solver=solver,
            parameter_uncertainty_method=parameter_uncertainty_method,
            update_inits=update_inits,
        ),
    )

    while len(model.estimation_steps) > 0:
        model = remove_estimation_step(model, 0)

    if est_settings:
        model = add_estimation_step(model, **est_settings)
    if eval_settings:
        model = add_estimation_step(model, **eval_settings)

    if solver:
        model = set_ode_solver(model, solver)

    candidate_entry = ModelEntry.create(model, modelfit_results=None, parent=model_entry.model)
    return candidate_entry


def _create_est_settings(method, parameter_uncertainty_method=None, add_eval_after_est=False):
    est_settings = {
        'method': method,
        'interaction': True,
        'laplace': False,
        'auto': True,
        'keep_every_nth_iter': 10,
        'maximum_evaluations': 9999,
    }

    if method == 'LAPLACE':
        est_settings['method'] = 'FOCE'
        est_settings['laplace'] = True

    if add_eval_after_est:
        est_settings['parameter_uncertainty_method'] = None
    else:
        est_settings['parameter_uncertainty_method'] = parameter_uncertainty_method

    return est_settings


def _create_eval_settings(laplace=False, parameter_uncertainty_method=None):
    eval_settings = {
        'method': 'IMP',
        'interaction': True,
        'evaluation': True,
        'laplace': False,
        'maximum_evaluations': 9999,
        'isample': 10000,
        'niter': 10,
        'keep_every_nth_iter': 10,
        'parameter_uncertainty_method': parameter_uncertainty_method,
    }

    if laplace:
        eval_settings['laplace'] = True

    return eval_settings


def _create_description(methods, solver, parameter_uncertainty_method, update_inits=False):
    model_description = ','.join(methods)
    if solver:
        model_description += f';{solver}'
    if parameter_uncertainty_method:
        model_description += f';{parameter_uncertainty_method}'
    if update_inits:
        model_description += ' (update_inits)'
    return model_description
