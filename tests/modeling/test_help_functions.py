import pytest

from pharmpy.modeling import fix_parameters
from pharmpy.modeling.help_functions import _as_integer, _get_etas


def test_get_etas(pheno_path, testdata, load_model_for_test):
    model = load_model_for_test(pheno_path)

    etas = _get_etas(model, ['ETA(1)'])
    assert len(etas) == 1

    etas = _get_etas(model, ['ETA(1)', 'CL'], include_symbols=True)
    assert len(etas) == 1

    etas = _get_etas(model, ['ETA(1)', 'V'], include_symbols=True)
    assert len(etas) == 2

    with pytest.raises(KeyError):
        _get_etas(model, ['ETA(23)'])

    model = load_model_for_test(testdata / 'nonmem' / 'pheno_block.mod')
    rvs = _get_etas(model, None)
    assert rvs[0] == 'ETA(1)'

    fix_parameters(model, ['OMEGA(1,1)'])
    rvs = _get_etas(model, None)
    assert rvs[0] == 'ETA(2)'

    model = load_model_for_test(testdata / 'nonmem' / 'pheno_block.mod')
    new_eta = model.random_variables['ETA(1)'].derive(level='IOV')
    model.random_variables = new_eta + model.random_variables[1:]
    rvs = _get_etas(model, None)
    assert rvs[0] == 'ETA(2)'


def test_as_integer():
    n = _as_integer(1)
    assert isinstance(n, int) and n == 1
    n = _as_integer(1.0)
    assert isinstance(n, int) and n == 1
    with pytest.raises(TypeError):
        _as_integer(1.5)
