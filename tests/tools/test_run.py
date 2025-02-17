import importlib
import inspect
import os
import shutil
from pathlib import Path

import pytest

import pharmpy
from pharmpy.deps import numpy as np
from pharmpy.results import read_results
from pharmpy.tools.run import (
    _create_metadata_common,
    _create_metadata_tool,
    _get_run_setup,
    rank_models,
    retrieve_final_model,
    retrieve_models,
    summarize_errors,
    summarize_modelfit_results,
)
from pharmpy.utils import TemporaryDirectoryChanger
from pharmpy.workflows import LocalDirectoryToolDatabase, local_dask


def test_create_metadata_tool():
    name = 'modelsearch'
    tool = importlib.import_module(f'pharmpy.tools.{name}')
    tool_params = inspect.signature(tool.create_workflow).parameters

    metadata = _create_metadata_tool(
        tool_name=name,
        tool_params=tool_params,
        tool_options={'iiv_strategy': 0},
        args=('ABSORPTION(ZO)', 'exhaustive'),
    )

    assert metadata['pharmpy_version'] == pharmpy.__version__
    assert metadata['tool_name'] == 'modelsearch'
    assert metadata['tool_options']['rank_type'] == 'bic'

    metadata = _create_metadata_tool(
        tool_name=name,
        tool_params=tool_params,
        tool_options={'algorithm': 'exhaustive'},
        args=('ABSORPTION(ZO)',),
    )

    assert metadata['tool_name'] == 'modelsearch'
    assert metadata['tool_options']['algorithm'] == 'exhaustive'

    with pytest.raises(Exception, match='modelsearch: \'algorithm\' was not set'):
        _create_metadata_tool(
            tool_name=name,
            tool_params=tool_params,
            tool_options={},
            args=('ABSORPTION(ZO)',),
        )


def test_get_run_setup(tmp_path):
    with TemporaryDirectoryChanger(tmp_path):
        name = 'modelsearch'

        dispatcher, database = _get_run_setup(common_options={}, toolname=name)

        assert dispatcher.__name__.endswith('local_dask')
        assert database.path.stem == 'modelsearch_dir1'

        dispatcher, database = _get_run_setup(
            common_options={'path': 'tool_database_path'}, toolname=name
        )

        assert dispatcher.__name__.endswith('local_dask')
        assert database.path.stem == 'tool_database_path'


def test_create_metadata_common(tmp_path):
    with TemporaryDirectoryChanger(tmp_path):
        name = 'modelsearch'

        dispatcher = local_dask
        database = LocalDirectoryToolDatabase(name)

        metadata = _create_metadata_common(
            common_options={}, dispatcher=dispatcher, database=database, toolname=name
        )

        assert metadata['dispatcher'] == 'pharmpy.workflows.dispatchers.local_dask'
        assert metadata['database']['class'] == 'LocalDirectoryToolDatabase'
        path = Path(metadata['database']['path'])
        assert path.stem == 'modelsearch_dir1'
        assert 'path' not in metadata.keys()

        path = 'tool_database_path'

        dispatcher = local_dask
        database = LocalDirectoryToolDatabase(name, path)

        metadata = _create_metadata_common(
            common_options={'path': path}, dispatcher=dispatcher, database=database, toolname=name
        )

        path = Path(metadata['database']['path'])
        assert path.stem == 'tool_database_path'
        assert metadata['path'] == 'tool_database_path'


def test_retrieve_models(testdata):
    tool_database_path = testdata / 'results' / 'tool_databases' / 'modelsearch'

    model_to_retrieve = ['modelsearch_run1']

    models = retrieve_models(tool_database_path, names=model_to_retrieve)
    assert len(models) == 1
    assert models[0].name == model_to_retrieve[0]

    model_names_all = [
        'input_model',
        'modelsearch_run1',
        'modelsearch_run2',
        'modelsearch_run3',
        'modelsearch_run4',
    ]

    models = retrieve_models(tool_database_path)
    assert [model.name for model in models] == model_names_all

    with open(tool_database_path / 'results.json') as f:
        results_json = f.read()
        if os.name == 'nt':
            new_path = str(tool_database_path).replace('\\', '\\\\')
        else:
            new_path = str(tool_database_path)

        results_json_testpath = results_json.replace('/tmp/tool_results/modelsearch', new_path)

    res = read_results(results_json_testpath)
    models = retrieve_models(res, names=model_to_retrieve)
    assert models[0].name == model_to_retrieve[0]

    tool_db = LocalDirectoryToolDatabase('modelsearch', path=tool_database_path, exist_ok=True)
    models = retrieve_models(tool_db, names=model_to_retrieve)
    assert models[0].name == model_to_retrieve[0]

    models = retrieve_models(tool_db.model_database, names=model_to_retrieve)
    assert models[0].name == model_to_retrieve[0]

    with pytest.raises(ValueError, match='Models {\'x\'} not in database'):
        retrieve_models(res, names=['x'])

    tool_without_db_in_results = testdata / 'results' / 'qa_results.json'
    res = read_results(tool_without_db_in_results)
    with pytest.raises(
        ValueError, match='Results type \'QAResults\' does not serialize tool database'
    ):
        retrieve_models(res, names=model_to_retrieve)


def test_retrieve_final_model(testdata):
    tool_database_path = testdata / 'results' / 'tool_databases' / 'modelsearch'

    with open(tool_database_path / 'results.json') as f:
        results_json = f.read()
        if os.name == 'nt':
            new_path = str(tool_database_path).replace('\\', '\\\\')
        else:
            new_path = str(tool_database_path)

        results_json_testpath = results_json.replace('/tmp/tool_results/modelsearch', new_path)

    res = read_results(results_json_testpath)
    final_model = retrieve_final_model(res)
    assert final_model.name == 'modelsearch_run2'

    results_json_none = results_json_testpath.replace(
        '"final_model_name": "modelsearch_run2"', '"final_model_name": null'
    )
    res = read_results(results_json_none)
    with pytest.raises(ValueError, match='Attribute \'final_model_name\' is None'):
        retrieve_final_model(res)


def test_summarize_errors(load_model_for_test, testdata, tmp_path, pheno_path):
    with TemporaryDirectoryChanger(tmp_path):
        model = load_model_for_test(pheno_path)
        shutil.copy2(testdata / 'pheno_data.csv', tmp_path)

        error_path = testdata / 'nonmem' / 'errors'

        shutil.copy2(testdata / 'nonmem' / 'pheno_real.mod', tmp_path / 'pheno_no_header.mod')
        shutil.copy2(error_path / 'no_header_error.lst', tmp_path / 'pheno_no_header.lst')
        shutil.copy2(testdata / 'nonmem' / 'pheno_real.ext', tmp_path / 'pheno_no_header.ext')
        model_no_header = load_model_for_test('pheno_no_header.mod')
        model_no_header.datainfo = model_no_header.datainfo.derive(path=tmp_path / 'pheno_data.csv')

        shutil.copy2(testdata / 'nonmem' / 'pheno_real.mod', tmp_path / 'pheno_rounding_error.mod')
        shutil.copy2(error_path / 'rounding_error.lst', tmp_path / 'pheno_rounding_error.lst')
        shutil.copy2(testdata / 'nonmem' / 'pheno_real.ext', tmp_path / 'pheno_rounding_error.ext')
        model_rounding_error = load_model_for_test('pheno_rounding_error.mod')
        model_rounding_error.datainfo = model_rounding_error.datainfo.derive(
            path=tmp_path / 'pheno_data.csv'
        )

        models = [model, model_no_header, model_rounding_error]
        summary = summarize_errors(models)

        assert 'pheno_real' not in summary.index.get_level_values('model')
        assert len(summary.loc[('pheno_no_header', 'WARNING')]) == 1
        assert len(summary.loc[('pheno_no_header', 'ERROR')]) == 2
        assert len(summary.loc[('pheno_rounding_error', 'ERROR')]) == 2


class DummyModel:
    def __init__(self, name, parent, parameter_names, **kwargs):
        self.name = name
        self.parameters = parameter_names
        self.parent_model = parent
        self.modelfit_results = DummyResults(**kwargs)


class DummyResults:
    def __init__(
        self, ofv, minimization_successful=True, termination_cause=None, significant_digits=5
    ):
        self.ofv = ofv
        self.minimization_successful = minimization_successful
        self.termination_cause = termination_cause
        # 5 is an arbitrary number, this is relevant in test if sig. digits is unreportable (NaN)
        self.significant_digits = significant_digits


def test_rank_models():
    base = DummyModel('base', parent='base', parameter_names=['p1'], ofv=0)
    m1 = DummyModel(
        'm1',
        parent='base',
        parameter_names=['p1', 'p2'],
        ofv=-5,
        minimization_successful=False,
        termination_cause='rounding_errors',
    )
    m2 = DummyModel('m2', parent='base', parameter_names=['p1', 'p2'], ofv=-4)
    m3 = DummyModel('m3', parent='base', parameter_names=['p1', 'p2', 'p3'], ofv=-4)
    m4 = DummyModel('m4', parent='base', parameter_names=['p1'], ofv=1)

    models = [m1, m2, m3, m4]

    df = rank_models(base, models, rank_type='ofv')
    assert len(df) == 5
    best_model = df.loc[df['rank'] == 1].index.values
    assert list(best_model) == ['m2', 'm3']

    # Test if rounding errors are allowed
    df = rank_models(base, models, errors_allowed=['rounding_errors'], rank_type='ofv')
    best_model = df.loc[df['rank'] == 1].index.values
    assert list(best_model) == ['m1']
    ranked_models = df.dropna().index.values
    assert len(ranked_models) == 5

    # Test with a cutoff of dOFV=1
    df = rank_models(base, models, rank_type='ofv', cutoff=1)
    ranked_models = df.dropna().index.values
    assert len(ranked_models) == 2

    # Test with LRT
    df = rank_models(base, models, rank_type='lrt', cutoff=0.05)
    ranked_models = list(df.dropna().index.values)
    assert sorted(ranked_models) == ['base', 'm2']

    # Test if candidate model does not have an OFV
    m5 = DummyModel('m5', parent='base', parameter_names=['p1'], ofv=np.nan)
    df = rank_models(base, models + [m5], rank_type='ofv')
    ranked_models = list(df.dropna().index.values)
    assert 'm5' not in ranked_models
    assert np.isnan(df.loc['m5']['rank'])

    # Test if model has minimized but has unreportable number of significant digits while still allowing rounding
    # errors
    m6 = DummyModel(
        'm6',
        parent='base',
        parameter_names=['p1'],
        ofv=-5,
        minimization_successful=False,
        termination_cause='rounding_errors',
        significant_digits=np.nan,
    )
    df = rank_models(base, models + [m6], errors_allowed=['rounding_errors'], rank_type='ofv')
    ranked_models = list(df.dropna().index.values)
    assert 'm6' not in ranked_models
    assert np.isnan(df.loc['m6']['rank'])

    # Test if base model failed, fall back to rank value
    base_nan = DummyModel('base_nan', parent='base_nan', parameter_names=['p1'], ofv=np.nan)
    df = rank_models(base_nan, models, errors_allowed=['rounding_errors'], rank_type='ofv')
    assert df.iloc[0].name == 'm1'


def test_summarize_modelfit_results(
    load_model_for_test, create_model_for_test, testdata, pheno_path
):
    pheno = load_model_for_test(pheno_path)

    summary_single = summarize_modelfit_results(pheno)

    assert summary_single.loc['pheno_real']['ofv'] == 586.2760562818805
    assert summary_single['OMEGA(1,1)_estimate'].mean() == 0.0293508

    assert len(summary_single.index) == 1

    mox = load_model_for_test(testdata / 'nonmem' / 'models' / 'mox1.mod')

    summary_multiple = summarize_modelfit_results([pheno, mox])

    assert summary_multiple.loc['mox1']['ofv'] == -624.5229577248352
    assert summary_multiple['OMEGA(1,1)_estimate'].mean() == 0.2236304
    assert summary_multiple['OMEGA(2,1)_estimate'].mean() == 0.395647  # One is NaN

    assert len(summary_multiple.index) == 2
    assert list(summary_multiple.index) == ['pheno_real', 'mox1']

    pheno_no_res = create_model_for_test(pheno.model_code)
    pheno_no_res.name = 'pheno_no_res'

    summary_no_res = summarize_modelfit_results([pheno, pheno_no_res])

    assert summary_no_res.loc['pheno_real']['ofv'] == 586.2760562818805
    assert np.isnan(summary_no_res.loc['pheno_no_res']['ofv'])
    assert np.all(np.isnan(summary_no_res.filter(regex='estimate$').loc['pheno_no_res']))

    pheno_multest = load_model_for_test(
        testdata
        / 'nonmem'
        / 'modelfit_results'
        / 'onePROB'
        / 'multEST'
        / 'noSIM'
        / 'pheno_multEST.mod'
    )

    summary_multest = summarize_modelfit_results([pheno_multest, mox])

    assert len(summary_multest.index) == 2

    assert not summary_multest.loc['pheno_multEST']['minimization_successful']
    summary_multest_full = summarize_modelfit_results(
        [pheno_multest, mox], include_all_estimation_steps=True
    )

    assert len(summary_multest_full.index) == 3
    assert len(set(summary_multest_full.index.get_level_values('model'))) == 2
    assert summary_multest_full.loc['pheno_multEST', 1]['run_type'] == 'estimation'
    assert summary_multest_full.loc['pheno_multEST', 2]['run_type'] == 'evaluation'

    assert not summary_multest_full.loc['pheno_multEST', 1]['minimization_successful']

    pheno_multest_no_res = create_model_for_test(pheno_multest.model_code)
    pheno_multest_no_res.name = 'pheno_multest_no_res'

    summary_multest_full_no_res = summarize_modelfit_results(
        [pheno_multest_no_res, mox], include_all_estimation_steps=True
    )

    assert summary_multest_full_no_res.loc['mox1', 1]['ofv'] == -624.5229577248352
    assert np.isnan(summary_multest_full_no_res.loc['pheno_multest_no_res', 1]['ofv'])
    estimates = summary_multest_full_no_res.loc['pheno_multest_no_res', 2].iloc[2:]
    assert estimates.isnull().all()


def test_summarize_modelfit_results_errors(load_model_for_test, testdata, tmp_path, pheno_path):
    with TemporaryDirectoryChanger(tmp_path):
        model = load_model_for_test(pheno_path)
        shutil.copy2(testdata / 'pheno_data.csv', tmp_path)

        error_path = testdata / 'nonmem' / 'errors'

        shutil.copy2(testdata / 'nonmem' / 'pheno_real.mod', tmp_path / 'pheno_no_header.mod')
        shutil.copy2(error_path / 'no_header_error.lst', tmp_path / 'pheno_no_header.lst')
        shutil.copy2(testdata / 'nonmem' / 'pheno_real.ext', tmp_path / 'pheno_no_header.ext')
        model_no_header = load_model_for_test('pheno_no_header.mod')
        model_no_header.datainfo = model_no_header.datainfo.derive(path=tmp_path / 'pheno_data.csv')

        shutil.copy2(testdata / 'nonmem' / 'pheno_real.mod', tmp_path / 'pheno_rounding_error.mod')
        shutil.copy2(error_path / 'rounding_error.lst', tmp_path / 'pheno_rounding_error.lst')
        shutil.copy2(testdata / 'nonmem' / 'pheno_real.ext', tmp_path / 'pheno_rounding_error.ext')
        model_rounding_error = load_model_for_test('pheno_rounding_error.mod')
        model_rounding_error.datainfo = model_rounding_error.datainfo.derive(
            path=tmp_path / 'pheno_data.csv'
        )

        models = [model, model_no_header, model_rounding_error]
        summary = summarize_modelfit_results(models)

        assert summary.loc['pheno_real']['errors_found'] == 0
        assert summary.loc['pheno_real']['warnings_found'] == 0
        assert summary.loc['pheno_no_header']['errors_found'] == 2
        assert summary.loc['pheno_no_header']['warnings_found'] == 1
        assert summary.loc['pheno_rounding_error']['errors_found'] == 2
        assert summary.loc['pheno_rounding_error']['warnings_found'] == 0
