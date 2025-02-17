import importlib
import json
import lzma
from pathlib import Path

from pharmpy.deps import altair as alt
from pharmpy.deps import pandas as pd
from pharmpy.model import Results


class ResultsJSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        json.JSONDecoder.__init__(self, object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, obj):
        # NOTE this hook will be called for every dict produced by the
        # base JSONDecoder. It will not be called on int, float, str, or list.
        module = None
        cls = None

        if '__module__' in obj:
            module = obj['__module__']
            del obj['__module__']

        if '__class__' in obj:
            cls = obj['__class__']
            del obj['__class__']

        # NOTE handling cls not None and module is None is kept for backwards
        # compatibility

        if cls is None and module is not None:
            raise ValueError('Cannot specify module without specifying class')

        if module is None or module.startswith('pandas.'):
            if cls == 'DataFrame':
                return pd.read_json(json.dumps(obj), orient='table', precise_float=True)
            elif cls == 'Series':
                name = None
                if '__name__' in obj:
                    name = obj['__name__']
                    del obj['__name__']
                series = pd.read_json(
                    json.dumps(obj), typ='series', orient='table', precise_float=True
                )
                if name is not None:
                    series.name = name
                return series

        if module is None or module.startswith('altair.'):
            if cls == 'vega-lite':
                return alt.Chart.from_dict(obj)

        if cls is not None and cls.endswith('Results'):
            if module is None:
                # NOTE kept for backwards compatibility: we guess the module
                # path based on the class name.
                tool_name = cls[:-7].lower()  # NOTE trim "Results" suffix
                tool_module = importlib.import_module(f'pharmpy.tools.{tool_name}')
                results_class = tool_module.results_class
            else:
                tool_module = importlib.import_module(module)
                results_class = getattr(tool_module, cls)

            return results_class.from_dict(obj)

        from pharmpy.workflows import LocalDirectoryToolDatabase, Log

        if cls is not None and cls == 'LocalDirectoryToolDatabase':
            return LocalDirectoryToolDatabase.from_dict(obj)

        if cls == 'PosixPath':
            return Path(obj)
        if cls == 'Log':
            return Log.from_dict(obj)

        return obj


def read_results(path_or_buf):
    if '{' in str(path_or_buf):  # Heuristic to determine if path or buffer
        s = path_or_buf
    else:
        path = Path(path_or_buf)
        if path.is_dir():
            path /= 'results.json'
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.name.endswith('.xz'):
            with lzma.open(path, 'r') as json_file:
                s = json_file.read().decode('utf-8')
        else:
            with open(path, 'r') as json_file:
                s = json_file.read()
    return ResultsJSONDecoder().decode(s)


class ModelfitResults(Results):
    """Base class for results from a modelfit operation

    Attributes
    ----------
    correlation_matrix : pd.DataFrame
        Correlation matrix of the population parameter estimates
    covariance_matrix : pd.DataFrame
        Covariance matrix of the population parameter estimates
    information_matrix : pd.DataFrame
        Fischer information matrix of the population parameter estimates
    evaluation_ofv : float
        The objective function value as if the model was evaluated. Currently
        workfs for classical estimation methods by taking the OFV of the first
        iteration.
    individual_ofv : pd.Series
        OFV for each individual
    individual_estimates : pd.DataFrame
        Estimates for etas
    individual_estimates_covariance : pd.Series
        Estimated covariance between etas
    parameter_estimates : pd.Series
        Population parameter estimates
    parameter_estimates_iterations : pd.DataFrame
        All recorded iterations for parameter estimates
    parameter_estimates_sdcorr : pd.Series
        Population parameter estimates with variability parameters as standard deviations and
        correlations
    residuals: pd.DataFrame
        Table of various residuals
    predictions: pd.DataFrame
        Table of various predictions
    estimation_runtime : float
        Runtime for one estimation step
    runtime_total : float
        Total runtime of estimation
    standard_errors : pd.Series
        Standard errors of the population parameter estimates
    standard_errors_sdcorr : pd.Series
        Standard errors of the population parameter estimates on standard deviation and correlation
        scale
    relative_standard_errors : pd.Series
        Relative standard errors of the population parameter estimates
    termination_cause : str
        The cause of premature termination. One of 'maxevals_exceeded' and 'rounding_errors'
    function_evaluations : int
        Number of function evaluations
    """

    def __init__(
        self,
        ofv=None,
        ofv_iterations=None,
        parameter_estimates=None,
        parameter_estimates_sdcorr=None,
        parameter_estimates_iterations=None,
        covariance_matrix=None,
        correlation_matrix=None,
        information_matrix=None,
        standard_errors=None,
        standard_errors_sdcorr=None,
        relative_standard_errors=None,
        minimization_successful=None,
        minimization_successful_iterations=None,
        estimation_runtime=None,
        estimation_runtime_iterations=None,
        individual_ofv=None,
        individual_estimates=None,
        individual_estimates_covariance=None,
        residuals=None,
        predictions=None,
        runtime_total=None,
        termination_cause=None,
        termination_cause_iterations=None,
        function_evaluations=None,
        function_evaluations_iterations=None,
        significant_digits=None,
        significant_digits_iterations=None,
        log_likelihood=None,
        log=None,
    ):
        self.ofv = ofv
        self.ofv_iterations = ofv_iterations
        self.parameter_estimates = parameter_estimates
        self.parameter_estimates_sdcorr = parameter_estimates_sdcorr
        self.parameter_estimates_iterations = parameter_estimates_iterations
        self.covariance_matrix = covariance_matrix
        self.correlation_matrix = correlation_matrix
        self.information_matrix = information_matrix
        self.standard_errors = standard_errors
        self.standard_errors_sdcorr = standard_errors_sdcorr
        self.relative_standard_errors = relative_standard_errors
        self.minimization_successful = minimization_successful
        self.minimization_successful_iterations = minimization_successful_iterations
        self.estimation_runtime = estimation_runtime
        self.estimation_runtime_iterations = estimation_runtime_iterations
        self.function_evaluations = function_evaluations
        self.function_evaluations_iterations = function_evaluations_iterations
        self.individual_estimates = individual_estimates
        self.individual_estimates_covariance = individual_estimates_covariance
        self.individual_ofv = individual_ofv
        self.residuals = residuals
        self.predictions = predictions
        self.runtime_total = runtime_total
        self.termination_cause = termination_cause
        self.termination_cause_iterations = termination_cause_iterations
        self.function_evaluations = function_evaluations
        self.significant_digits = significant_digits
        self.significant_digits_iterations = significant_digits_iterations
        self.log_likelihood = log_likelihood
        self.log = log
