import sys

import pytest

from pharmpy.modeling import load_example_model, predict_outliers


@pytest.mark.skipif(sys.version_info >= (3, 10), reason="Skipping tests requiring tflite for Python 3.10")
def test_predict_outliers():
    model = load_example_model('pheno')
    res = predict_outliers(model)
    assert len(res) == 59
    assert res['residual'][1] == -0.27215176820755005
