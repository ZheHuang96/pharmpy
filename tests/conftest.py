from pathlib import Path
from typing import Dict, Hashable, Optional, Union

import pytest


@pytest.fixture(scope='session')
def testdata():
    """Test data (root) folder."""
    return Path(__file__).resolve().parent / 'testdata'


@pytest.fixture(scope='session')
def datadir(testdata):
    return testdata / 'nonmem'


@pytest.fixture(scope='session')
def pheno_path(datadir):
    return datadir / 'pheno_real.mod'


@pytest.fixture(scope='session')
def pheno(load_model_for_test, pheno_path):
    return load_model_for_test(pheno_path)


@pytest.fixture(scope='session')
def load_model_for_test(tmp_path_factory):

    from pharmpy.model import Model

    _cache: Dict[Hashable, Model] = {}

    def _load(given_path: Union[str, Path]) -> Model:
        # TODO Cache based on file contents instead.

        def _parse_model():
            return Model.create_model(given_path)

        basetemp = tmp_path_factory.getbasetemp().resolve()

        resolved_path = Path(given_path).resolve()

        try:
            # NOTE This skips caching when we are reading from a temporary
            # directory.
            resolved_path.relative_to(basetemp)
            return _parse_model()
        except ValueError:
            # NOTE This is raised when resolved_path is not descendant of
            # basetemp. With Python >= 3.9 we could use is_relative_to instead.
            pass

        from pharmpy.plugins.nonmem import conf

        key = (str(conf), str(resolved_path))

        if key not in _cache:
            _cache[key] = _parse_model()

        return _cache[key].copy()

    return _load


@pytest.fixture(scope='session')
def load_example_model_for_test():

    from pharmpy.model import Model
    from pharmpy.modeling import load_example_model

    _cache: Dict[Hashable, Model] = {}

    def _load(given_name: str) -> Model:
        def _parse_model():
            return load_example_model(given_name)

        from pharmpy.plugins.nonmem import conf

        key = (str(conf), given_name)

        if key not in _cache:
            _cache[key] = _parse_model()

        return _cache[key].copy()

    return _load


@pytest.fixture(scope='session')
def create_model_for_test(load_example_model_for_test):

    from io import StringIO

    from pharmpy.model import Model

    def _create(code: str, dataset: Optional[str] = None) -> Model:
        model = Model.create_model(StringIO(code))
        if dataset is not None:
            # NOTE This yields a copy of the dataset through Model#copy
            model.dataset = load_example_model_for_test(dataset).dataset
        return model

    return _create
