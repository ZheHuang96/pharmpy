import json
import os
import os.path
import subprocess
import uuid
from itertools import repeat
from pathlib import Path

from pharmpy.model import EstimationSteps
from pharmpy.modeling import write_csv, write_model
from pharmpy.plugins.nonmem import conf, convert_model, parse_modelfit_results

PARENT_DIR = f'..{os.path.sep}'


def execute_model(model, db):
    database = db.model_database
    parent_model = model.parent_model
    model = convert_model(model)
    model.parent_model = parent_model
    path = Path.cwd() / f'NONMEM_run_{model.name}-{uuid.uuid1()}'

    # NOTE This deduplicates the dataset before running NONMEM so we know which
    # filename to give to this dataset.
    database.store_model(model)
    # NOTE We cannot reuse model_with_correct_datapath as the model object
    # later because it might have lost some of the ETA names mapping due to the
    # current incomplete implementation of serialization of Pharmpy Model
    # objects through the NONMEM plugin. Hopefully we can get rid of this
    # hack later.
    model_with_correct_datapath = database.retrieve_model(model.name)
    stream = model_with_correct_datapath.internals.control_stream
    data_record = stream.get_records('DATA')[0]
    relative_dataset_path = data_record.filename

    # NOTE We setup a directory tree that replicates the structure generated by
    # the database so that NONMEM writes down the correct relative paths in
    # generated files such as results.lst.
    # NOTE It is important that we do this in a DB-agnostic way so that we do
    # not depent on its implementation.
    depth = relative_dataset_path.count(PARENT_DIR)
    # NOTE This creates a FS tree branch x/x/x/x/...
    model_path = path.joinpath(*repeat('x', depth))
    meta = model_path / '.pharmpy'
    meta.mkdir(parents=True, exist_ok=True)
    # NOTE This removes the leading ../
    relative_dataset_path_suffix = relative_dataset_path[len(PARENT_DIR) * depth :]
    # NOTE We do not support non-leading ../, e.g. a/b/../c
    assert PARENT_DIR not in relative_dataset_path_suffix
    dataset_path = path / Path(relative_dataset_path_suffix)
    datasets_path = dataset_path.parent
    datasets_path.mkdir(parents=True, exist_ok=True)

    # NOTE Write dataset and model files so they can be used by NONMEM.
    write_csv(model, path=dataset_path, force=True)
    model._dataset_updated = True  # Hack to get update_source to update IGNORE
    write_model(model, path=model_path, force=True)

    args = nmfe(
        model.name + model.filename_extension,
        'results.lst',
    )

    stdout = model_path / 'stdout'
    stderr = model_path / 'stderr'

    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, stderr=err, stdout=out, cwd=str(model_path)
        )

    basename = Path(model.name)
    (model_path / 'results.lst').rename((model_path / basename).with_suffix('.lst'))

    metadata = {
        'plugin': 'nonmem',
        'path': str(model_path),
    }

    plugin = {
        'commands': [
            {
                'args': args,
                'returncode': result.returncode,
            }
        ]
    }

    with database.transaction(model) as txn:

        txn.store_model()

        for suffix in ['.lst', '.ext', '.phi', '.cov', '.cor', '.coi']:
            txn.store_local_file((model_path / basename).with_suffix(suffix))

        for rec in model.internals.control_stream.get_records('TABLE'):
            txn.store_local_file(model_path / rec.path)

        txn.store_local_file(stdout)
        txn.store_local_file(stderr)

        plugin_path = model_path / 'nonmem.json'
        with open(plugin_path, 'w') as f:
            json.dump(plugin, f, indent=2)

        txn.store_local_file(plugin_path)

        txn.store_metadata(metadata)
        if len(model.estimation_steps) > 0:
            txn.store_modelfit_results()

            # Read in results for the server side
            model.modelfit_results = parse_modelfit_results(model, model_path / basename)

    return model


def nmfe_path():
    if os.name == 'nt':
        nmfe_candidates = ['nmfe74.bat', 'nmfe75.bat', 'nmfe73.bat']
    else:
        nmfe_candidates = ['nmfe74', 'nmfe75', 'nmfe73']
    path = conf.default_nonmem_path
    if path != Path(''):
        path /= 'run'
    for nmfe in nmfe_candidates:
        candidate_path = path / nmfe
        if candidate_path.is_file():
            path = candidate_path
            break
    else:
        raise FileNotFoundError(f'Cannot find nmfe script for NONMEM ({path})')
    return str(path)


def nmfe(*args):
    conf_args = []
    if conf.licfile is not None:
        conf_args.append(f'-licfile={str(conf.licfile)}')

    return [
        nmfe_path(),
        *args,
        *conf_args,
    ]


def evaluate_design(context, model):
    # Prepare and run model for design evaluation
    model = model.copy()
    model.name = '_design_model'

    model.estimation_steps = EstimationSteps()
    stream = model.internals.control_stream
    estrecs = stream.get_records('ESTIMATION')
    stream.remove_records(estrecs)

    design_code = '$DESIGN APPROX=FOCEI MODE=1 NELDER FIMDIAG=0 DATASIM=1 GROUPSIZE=32 OFVTYPE=0'
    stream.insert_record(design_code)

    execute_model(model, context)

    from pharmpy.tools.evaldesign import EvalDesignResults

    mfr = model.modelfit_results
    res = EvalDesignResults(
        ofv=mfr.ofv,
        individual_ofv=mfr.individual_ofv,
        information_matrix=mfr.information_matrix,
    )
    return res
