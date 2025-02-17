import pytest

from pharmpy.modeling import (
    add_iiv,
    add_peripheral_compartment,
    add_pk_iiv,
    create_joint_distribution,
)
from pharmpy.tools.iivsearch.algorithms import (
    _create_param_dict,
    _get_eta_combinations,
    _is_current_block_structure,
    brute_force_block_structure,
    create_eta_blocks,
)
from pharmpy.tools.iivsearch.tool import create_workflow, validate_input
from pharmpy.workflows import Workflow


@pytest.mark.parametrize(
    'list_of_parameters, block_structure, no_of_models',
    [([], [], 4), (['QP1'], [], 14), ([], ['ETA(1)', 'ETA(2)'], 4)],
)
def test_brute_force_block_structure(
    load_model_for_test, testdata, list_of_parameters, block_structure, no_of_models
):
    model = load_model_for_test(testdata / 'nonmem' / 'models' / 'mox2.mod')
    add_peripheral_compartment(model)
    add_iiv(model, list_of_parameters, 'add')
    if block_structure:
        create_joint_distribution(
            model, block_structure, individual_estimates=model.modelfit_results.individual_estimates
        )

    wf = brute_force_block_structure(model)
    fit_tasks = [task.name for task in wf.tasks if task.name.startswith('run')]

    assert len(fit_tasks) == no_of_models


def test_get_eta_combinations_4_etas(load_model_for_test, pheno_path):
    model = load_model_for_test(pheno_path)
    add_iiv(model, ['TVCL', 'TVV'], 'exp')

    eta_combos = _get_eta_combinations(model.random_variables.iiv)
    assert len(eta_combos) == 15

    block_combos = _get_eta_combinations(model.random_variables.iiv, as_blocks=True)

    assert len(block_combos) == 15

    combos_unique = [(tuple(i) for i in combo) for combo in block_combos]
    assert len(combos_unique) == 15

    len_of_combos = [list(map(lambda i: len(i), combo)) for combo in block_combos]
    assert len_of_combos.count([4]) == 1
    assert len_of_combos.count([1, 3]) == 4
    assert len_of_combos.count([2, 2]) == 3
    assert len_of_combos.count([1, 1, 2]) == 6
    assert len_of_combos.count([1, 1, 1, 1]) == 1


def test_get_eta_combinations_5_etas(load_model_for_test, pheno_path):
    model = load_model_for_test(pheno_path)
    add_iiv(model, ['TVCL', 'TVV', 'TAD'], 'exp')

    eta_combos = _get_eta_combinations(model.random_variables.iiv)
    assert len(eta_combos) == 31

    block_combos = _get_eta_combinations(model.random_variables.iiv, as_blocks=True)
    assert len(block_combos) == 52

    combos_unique = [(tuple(i) for i in combo) for combo in block_combos]
    assert len(combos_unique) == 52

    len_of_combos = [list(map(lambda i: len(i), combo)) for combo in block_combos]
    assert len_of_combos.count([5]) == 1
    assert len_of_combos.count([1, 4]) == 5
    assert len_of_combos.count([2, 3]) == 10
    assert len_of_combos.count([1, 1, 3]) == 10
    assert len_of_combos.count([1, 2, 2]) == 15
    assert len_of_combos.count([1, 1, 1, 2]) == 10
    assert len_of_combos.count([1, 1, 1, 1, 1]) == 1


def test_is_current_block_structure(load_model_for_test, pheno_path):
    model = load_model_for_test(pheno_path)
    add_iiv(model, ['TVCL', 'TVV'], 'exp')

    eta_combos = [['ETA(1)', 'ETA(2)'], ['ETA_TVCL'], ['ETA_TVV']]
    create_joint_distribution(
        model, eta_combos[0], individual_estimates=model.modelfit_results.individual_estimates
    )
    etas = model.random_variables.iiv
    assert _is_current_block_structure(etas, eta_combos)

    eta_combos = [['ETA(1)'], ['ETA(2)'], ['ETA_TVCL', 'ETA_TVV']]
    assert not _is_current_block_structure(etas, eta_combos)

    eta_combos = [['ETA(1)'], ['ETA(2)', 'ETA_TVCL'], ['ETA_TVV']]
    assert not _is_current_block_structure(etas, eta_combos)

    create_joint_distribution(
        model, individual_estimates=model.modelfit_results.individual_estimates
    )
    eta_combos = [['ETA(1)', 'ETA(2)', 'ETA_TVCL', 'ETA_TVV']]
    etas = model.random_variables.iiv
    assert _is_current_block_structure(etas, eta_combos)


def test_create_joint_dist(load_model_for_test, testdata):
    model = load_model_for_test(testdata / 'nonmem' / 'models' / 'mox2.mod')
    add_peripheral_compartment(model)
    add_pk_iiv(model)
    eta_combos = [['ETA(1)', 'ETA(2)'], ['ETA_QP1'], ['ETA_VP1']]
    create_eta_blocks(eta_combos, model)
    assert len(model.random_variables.iiv) == 4

    model = load_model_for_test(testdata / 'nonmem' / 'models' / 'mox2.mod')
    add_peripheral_compartment(model)
    add_pk_iiv(model)
    create_joint_distribution(
        model,
        ['ETA(1)', 'ETA(2)'],
        individual_estimates=model.modelfit_results.individual_estimates,
    )
    eta_combos = [['ETA(1)'], ['ETA(2)'], ['ETA(3)', 'ETA_VP1', 'ETA_QP1']]
    create_eta_blocks(eta_combos, model)
    assert len(model.random_variables.iiv) == 3


def test_get_param_names(create_model_for_test, load_model_for_test, testdata):
    model = load_model_for_test(testdata / 'nonmem' / 'models' / 'mox2.mod')

    param_dict = _create_param_dict(model, model.random_variables.iiv)
    param_dict_ref = {'ETA(1)': 'CL', 'ETA(2)': 'VC', 'ETA(3)': 'MAT'}

    assert param_dict == param_dict_ref

    model_code = model.model_code.replace(
        'CL = THETA(1) * EXP(ETA(1))', 'ETA_1 = ETA(1)\nCL = THETA(1) * EXP(ETA_1)'
    )
    model = create_model_for_test(model_code)

    param_dict = _create_param_dict(model, model.random_variables.iiv)

    assert param_dict == param_dict_ref


def test_create_workflow():
    assert isinstance(create_workflow('brute_force'), Workflow)


def test_create_workflow_with_model(load_model_for_test, testdata):
    model = load_model_for_test(testdata / 'nonmem' / 'pheno.mod')
    assert isinstance(create_workflow('brute_force', model=model), Workflow)


def test_validate_input():
    validate_input('brute_force')


def test_validate_input_with_model(load_model_for_test, testdata):
    model = load_model_for_test(testdata / 'nonmem' / 'pheno.mod')
    validate_input('brute_force', model=model)


@pytest.mark.parametrize(
    ('model_path', 'arguments', 'exception', 'match'),
    [
        (None, dict(algorithm=1), TypeError, 'Invalid `algorithm`'),
        (None, dict(algorithm='brute_force_no_of_eta'), ValueError, 'Invalid `algorithm`'),
        (None, dict(rank_type=1), TypeError, 'Invalid `rank_type`'),
        (None, dict(rank_type='bi'), ValueError, 'Invalid `rank_type`'),
        (None, dict(iiv_strategy=['no_add']), TypeError, 'Invalid `iiv_strategy`'),
        (None, dict(iiv_strategy='diagonal'), ValueError, 'Invalid `iiv_strategy`'),
        (None, dict(cutoff='1'), TypeError, 'Invalid `cutoff`'),
        (
            None,
            dict(model=1),
            TypeError,
            'Invalid `model`',
        ),
    ],
)
def test_validate_input_raises(
    load_model_for_test,
    testdata,
    model_path,
    arguments,
    exception,
    match,
):

    model = load_model_for_test(testdata.joinpath(*model_path)) if model_path else None

    harmless_arguments = dict(
        algorithm='brute_force',
    )

    kwargs = {**harmless_arguments, 'model': model, **arguments}

    with pytest.raises(exception, match=match):
        validate_input(**kwargs)
