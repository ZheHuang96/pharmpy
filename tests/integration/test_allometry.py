import os.path
import shutil

from pharmpy.model import Model
from pharmpy.tools import run_tool
from pharmpy.utils import TemporaryDirectoryChanger
from pharmpy.workflows import ModelDatabase


def test_allometry(tmp_path, testdata):
    with TemporaryDirectoryChanger(tmp_path):
        for path in (testdata / 'nonmem').glob('pheno_real.*'):
            shutil.copy2(path, tmp_path)
        shutil.copy2(testdata / 'nonmem' / 'pheno.dta', tmp_path)
        shutil.copy2(testdata / 'nonmem' / 'sdtab1', tmp_path)

        model = Model.create_model('pheno_real.mod')
        model.datainfo = model.datainfo.derive(path=tmp_path / 'pheno.dta')
        res = run_tool('allometry', model, allometric_variable='WGT')
        assert len(res.summary_models) == 2

        db: ModelDatabase = res.tool_database.model_database
        sep = os.path.sep
        model_name = 'scaled_model'
        assert str(db.retrieve_model(model_name).datainfo.path).endswith(
            f'{sep}allometry_dir1{sep}models{sep}.datasets{sep}input_model.csv'
        )
        path = db.retrieve_file(model_name, f'{model_name}.lst')
        with open(path, 'r') as fh:
            while line := fh.readline():
                # NOTE skip date, time, description etc
                if line[:6] == '$DATA ':
                    assert line == f'$DATA ..{sep}.datasets{sep}input_model.csv IGNORE=@\n'
                    break
