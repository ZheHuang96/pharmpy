import re

from pharmpy.modeling import add_iiv, copy_model, create_joint_distribution, update_inits
from pharmpy.tools.modelfit import create_fit_workflow
from pharmpy.workflows import Task, Workflow

from .mfl import ModelFeatures


def exhaustive(mfl, add_iivs, iiv_as_fullblock, add_mdt_iiv):
    features = ModelFeatures(mfl)
    wf_search = Workflow()

    model_tasks = []
    model_features = dict()

    combinations = list(features.all_combinations())
    funcs = features.all_funcs()

    for i, combo in enumerate(combinations, 1):
        model_name = f'modelsearch_candidate{i}'

        task_copy = Task('copy', _copy, model_name)
        wf_search.add_task(task_copy)

        task_previous = task_copy
        for feat in combo:
            func = funcs[feat]
            task_function = Task(feat, func)
            wf_search.add_task(task_function, predecessors=task_previous)
            if add_iivs:
                task_add_iiv = Task(
                    'add_iivs', _add_iiv_to_func, feat, iiv_as_fullblock, add_mdt_iiv
                )
                wf_search.add_task(task_add_iiv, predecessors=task_function)
                task_previous = task_add_iiv
            else:
                task_previous = task_function

        wf_fit = create_fit_workflow(n=1)
        wf_search.insert_workflow(wf_fit, predecessors=task_previous)

        model_features[model_name] = tuple(combo)

        model_tasks += wf_fit.output_tasks

    return wf_search, model_tasks, model_features


def exhaustive_stepwise(mfl, add_iivs, iiv_as_fullblock, add_mdt_iiv):
    mfl_features = ModelFeatures(mfl)
    mfl_funcs = mfl_features.all_funcs()

    wf_search = Workflow()
    model_tasks = []
    model_features = dict()

    while True:
        no_of_trans = 0
        actions = _get_possible_actions(wf_search, mfl_features)
        for task_parent, feat_new in actions.items():
            for feat in feat_new:
                model_name = f'modelsearch_candidate{len(model_tasks) + 1}'

                task_copy = Task('copy', _copy, model_name)
                if task_parent:
                    wf_search.add_task(task_copy, predecessors=[task_parent])
                else:
                    wf_search.add_task(task_copy)

                wf_stepwise_step, task_transformed = _create_stepwise_workflow(
                    feat, mfl_funcs[feat], add_iivs, iiv_as_fullblock, add_mdt_iiv
                )

                wf_search.insert_workflow(wf_stepwise_step, predecessors=task_copy)
                model_tasks += wf_stepwise_step.output_tasks

                features_previous = _get_previous_features(wf_search, task_transformed, mfl_funcs)
                model_features[model_name] = tuple(list(features_previous) + [feat])

                no_of_trans += 1
        if no_of_trans == 0:
            break

    return wf_search, model_tasks, model_features


def _get_possible_actions(wf, mfl_features):
    actions = dict()
    if wf.output_tasks:
        tasks = wf.output_tasks
    else:
        tasks = ['']
    for task in tasks:
        mfl_funcs = mfl_features.all_funcs()
        if task:
            feat_previous = _get_previous_features(wf, task, mfl_funcs)
        else:
            feat_previous = dict()

        trans_possible = [
            feat
            for feat, func in mfl_funcs.items()
            if _is_allowed(feat, func, feat_previous, mfl_features)
        ]

        actions[task] = trans_possible
    return actions


def _get_previous_features(wf, task, mfl_funcs):
    tasks_upstream = wf.get_upstream_tasks(task)
    tasks_upstream.reverse()
    features_previous = [task.name for task in tasks_upstream if task.name in mfl_funcs.keys()]
    return features_previous


def _create_stepwise_workflow(feat, func, add_iivs, iiv_as_fullblock, add_mdt_iiv):
    wf_stepwise_step = Workflow()

    task_update_inits = Task('update_inits', _update_initial_estimates)
    wf_stepwise_step.add_task(task_update_inits)

    task_function = Task(feat, func)
    wf_stepwise_step.add_task(task_function, predecessors=task_update_inits)

    if add_iivs or add_mdt_iiv:
        task_add_iiv = Task('add_iivs', _add_iiv_to_func, feat, iiv_as_fullblock, add_mdt_iiv)
        wf_stepwise_step.add_task(task_add_iiv, predecessors=task_function)
        task_transformed = task_add_iiv
    else:
        task_transformed = task_function

    wf_fit = create_fit_workflow(n=1)
    wf_stepwise_step.insert_workflow(wf_fit, predecessors=task_transformed)

    return wf_stepwise_step, task_transformed


def _is_allowed(feat_current, func_current, feat_previous, mfl_features):
    mfl_funcs = mfl_features.all_funcs()
    func_type = mfl_features.get_funcs_same_type(feat_current)
    # Check if current function is in previous transformations
    if feat_current in feat_previous:
        return False
    # Check if peripheral transformation is allowed
    if feat_current.startswith('PERIPHERALS'):
        peripheral_previous = [
            mfl_funcs[feat] for feat in feat_previous if feat.startswith('PERIPHERALS')
        ]
        return _is_allowed_peripheral(func_current, peripheral_previous, mfl_features)
    # Check if any functions of the same type has been used
    if any(mfl_funcs[feat] in func_type for feat in feat_previous):
        return False
    # No transformations have been made
    if not feat_previous:
        return True
    # Combinations to skip
    not_supported_combo = [
        ('ABSORPTION(ZO)', 'TRANSITS'),
        ('ABSORPTION(SEQ-ZO-FO)', 'TRANSITS'),
        ('ABSORPTION(SEQ-ZO-FO)', 'LAGTIME'),
        ('LAGTIME', 'TRANSITS'),
    ]
    for feat_1, feat_2 in not_supported_combo:
        if any(
            (feat_current.startswith(feat_1) and feat.startswith(feat_2))
            or (feat_current.startswith(feat_2) and feat.startswith(feat_1))
            for feat in feat_previous
        ):
            return False
    return True


def _is_allowed_peripheral(func_current, peripheral_previous, mfl_features):
    n_all = list(mfl_features.peripherals.args)
    n = func_current.keywords['n']
    if peripheral_previous:
        n_prev = [func.keywords['n'] for func in peripheral_previous]
    else:
        n_prev = []
    if not n_prev:
        if n == min(n_all):
            return True
        else:
            return False
    n_index = n_all.index(n)
    if n_index > 0 and n_all[n_index - 1] < n:
        return True
    return False


def _copy(name, model):
    model_copy = copy_model(model, name)
    return model_copy


def _update_initial_estimates(model):
    # FIXME: this should use dynamic workflows and not dispatch the next task
    try:
        update_inits(model)
    except ValueError:
        pass
    return model


def _add_iiv_to_func(feat, iiv_as_fullblock, add_mdt_iiv, model):
    eta_dict = {
        'ABSORPTION(ZO)': ['MAT'],
        'ABSORPTION(SEQ-ZO-FO)': ['MAT', 'MDT'],
        'ELIMINATION(ZO)': ['CLMM', 'KM'],
        'ELIMINATION(MM)': ['CLMM', 'KM'],
        'ELIMINATION(MIX-FO-MM)': ['CLMM', 'KM'],
        'LAGTIME()': ['MDT'],
    }
    parameters = []
    if add_mdt_iiv and (feat == 'LAGTIME()' or feat.startswith('TRANSITS')):
        parameters = ['MDT']
    else:
        if feat in eta_dict.keys():
            parameters = eta_dict[feat]
        elif feat.startswith('TRANSITS'):
            parameters = ['MDT']
        elif feat.startswith('PERIPHERALS'):
            no_of_peripherals = re.search(r'PERIPHERALS\((\d+)\)', feat).group(1)
            parameters = [f'VP{i}' for i in range(1, int(no_of_peripherals) + 1)] + [
                f'QP{i}' for i in range(1, int(no_of_peripherals) + 1)
            ]

    for param in parameters:
        assignment = model.statements.find_assignment(param)
        etas = {eta.name for eta in assignment.rhs_symbols}
        if etas.intersection(model.random_variables.etas.names):
            continue
        try:
            add_iiv(model, param, 'exp')
        except ValueError as e:
            if not str(e).startswith('Cannot insert parameter with already existing name'):
                raise
    if iiv_as_fullblock:
        create_joint_distribution(model)

    return model
