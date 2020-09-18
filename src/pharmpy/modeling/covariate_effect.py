import math
import re
from operator import add, mul

import numpy as np
import sympy
from sympy import Eq, Float, Gt, Le, Piecewise, exp

from pharmpy.parameter import Parameter
from pharmpy.statements import Assignment
from pharmpy.symbols import symbol as S


def add_covariate_effect(model, parameter, covariate, effect, operation='*'):
    """
    Adds covariate effect to :class:`pharmpy.model`. The following effects have templates:

    - Linear function for continuous covariates (*lin*)
    - Linear function for categorical covariates (*cat*)
    - Piecewise linear function/"hockey-stick", continuous covariates only (*piece_lin*)
    - Exponential function, continuous covariates only (*exp*)
    - Power function, continuous covariates only (*pow*)

    Parameters
    ----------
    model : Model
        Pharmpy model to add covariate effect to.
    parameter : str
        Name of parameter to add covariate effect to.
    covariate : str
        Name of covariate.
    effect : str
        Type of covariate effect. May be abbreviated covariate effect (see above) or custom.
    operation : str, optional
        Whether the covariate effect should be added or multiplied (default).
    """
    mean = _calculate_mean(model.dataset, covariate)
    median = _calculate_median(model.dataset, covariate)
    std = _calculate_std(model.dataset, covariate)

    thetas = _create_thetas(model, effect, covariate)
    covariate_effect = _create_template(effect, model, covariate)

    sset = model.statements
    param_statement = sset.find_assignment(parameter)

    param_index = sset.index(param_statement)

    covariate_effect.apply(parameter, covariate, thetas)
    effect_statement = covariate_effect.create_effect_statement(operation, param_statement)

    if effect != 'cat':
        statistic_statement = covariate_effect.create_statistics_statement(covariate, mean,
                                                                           median, std)
        sset.insert(param_index + 1, statistic_statement)
        param_index += 1

    sset.insert(param_index + 1, covariate_effect.template)
    sset.insert(param_index + 2, effect_statement)

    model.statements = sset


def _create_thetas(model, effect, covariate):
    """Creates theta parameters and adds to parameter set of model.

    Number of parameters depends on which covariate effect."""
    if effect == 'piece_lin':
        no_of_thetas = 2
    elif effect == 'cat':
        no_of_thetas = _count_categorical(model, covariate).nunique()
    else:
        no_of_thetas = 1

    pset = model.parameters

    theta_names = dict()
    theta_name = str(model.create_symbol(stem='COVEFF', force_numbering=True))

    if no_of_thetas == 1:
        inits = _choose_param_inits(effect, model.dataset, covariate)

        init = inits['init']
        upper = inits['upper']
        lower = inits['lower']

        pset.add(Parameter(theta_name, init, lower, upper))
        theta_names['theta'] = theta_name
    else:
        cov_eff_number = int(re.findall(r'\d', theta_name)[0])

        for i in range(1, no_of_thetas+1):
            inits = _choose_param_inits(effect, model.dataset, covariate, i)

            init = inits['init']
            upper = inits['upper']
            lower = inits['lower']

            pset.add(Parameter(theta_name, init, lower, upper))
            theta_names[f'theta{i}'] = theta_name
            theta_name = f'COVEFF{cov_eff_number + i}'

    model.parameters = pset

    return theta_names


def _count_categorical(model, covariate):
    """Gets most common level per individual of specified covariate."""
    data = model.dataset.groupby('ID')[covariate]
    counts = data.agg(lambda ids: ids.value_counts(dropna=False).index[0])

    return counts


def _calculate_mean(df, covariate, baselines=False):
    """Calculate mean. Can be set to use baselines, otherwise it is
    calculated first per individual, then for the group."""
    if baselines:
        return df[str(covariate)].mean()
    else:
        return df.groupby('ID')[str(covariate)].mean().mean()


def _calculate_median(df, covariate, baselines=False):
    """Calculate median. Can be set to use baselines, otherwise it is
    calculated first per individual, then for the group."""
    if baselines:
        return df.pharmpy.baselines[str(covariate)].median()
    else:
        return df.groupby('ID')[str(covariate)].median().median()


def _calculate_std(df, covariate, baselines=False):
    """Calculate median. Can be set to use baselines, otherwise it is
    calculated first per individual, then for the group."""
    if baselines:
        return df.pharmpy.baselines[str(covariate)].std()
    else:
        return df.groupby('ID')[str(covariate)].mean().std()


def _choose_param_inits(effect, df, covariate, index=None):
    """Chooses inits for parameters. If the effect is exponential, the
    bounds need to be dynamic."""
    init_default = 0.001

    inits = dict()

    cov_median = _calculate_median(df, covariate)
    cov_min = df[str(covariate)].min()
    cov_max = df[str(covariate)].max()

    lower, upper = _choose_bounds(effect, cov_median, cov_min, cov_max, index)

    if effect == 'exp':
        if lower > init_default or init_default > upper:
            init = (upper + lower)/2
            if init == 0:
                init = upper/5
        else:
            init = init_default
    elif effect == 'pow':
        init = init_default
    else:
        init = init_default

    inits['init'] = init
    inits['lower'] = lower
    inits['upper'] = upper

    return inits


def _choose_bounds(effect, cov_median, cov_min, cov_max, index=None):
    if effect == 'exp':
        min_diff = cov_min - cov_median
        max_diff = cov_max - cov_median

        lower_expected = 0.01
        upper_expected = 100

        if min_diff == 0 or max_diff == 0:
            return lower_expected, upper_expected
        else:
            log_base = 10
            lower = max(math.log(lower_expected, log_base)/max_diff,
                        math.log(upper_expected, log_base)/min_diff)
            upper = min(math.log(lower_expected, log_base)/min_diff,
                        math.log(upper_expected, log_base)/max_diff)
    elif effect == 'lin':
        if cov_median == cov_min:
            upper = 100000
        else:
            upper = 1 / (cov_median - cov_min)
        if cov_median == cov_max:
            lower = -100000
        else:
            lower = 1 / (cov_median - cov_max)
    elif effect == 'piece_lin':
        if cov_median == cov_min or cov_median == cov_max:
            raise Exception('Median cannot be same as min or max, cannot use '
                            'piecewise-linear parameterization.')
        if index == 0:
            upper = 1 / (cov_median - cov_min)
            lower = -100000
        else:
            upper = 100000
            lower = 1 / (cov_median - cov_max)
    elif effect == 'pow':
        return -100, 100000
    else:
        return -1000000, 100000
    return lower, upper


def _create_template(effect, model, covariate):
    """Creates Covariate class objects with effect template."""
    if effect == 'lin':
        return CovariateEffect.linear()
    elif effect == 'cat':
        counts = _count_categorical(model, covariate)
        return CovariateEffect.categorical(counts)
    elif effect == 'piece_lin':
        return CovariateEffect.piecewise_linear()
    elif effect == 'exp':
        return CovariateEffect.exponential()
    elif effect == 'pow':
        return CovariateEffect.power()
    else:
        symbol = S('symbol')
        expression = sympy.sympify(effect)
        return CovariateEffect(Assignment(symbol, expression))


class CovariateEffect:
    """
    Covariate effect consisting of new assignments.

    Attributes
    ----------
    template
        Assignment based on covariate effect
    statistic_type
        Mean or median, depends on which covariate effect

    :meta private:

    """
    def __init__(self, template):
        self.template = template
        self.statistic_type = None

    def apply(self, parameter, covariate, thetas):
        effect_name = f'{parameter}{covariate}'
        self.template.symbol = S(effect_name)

        self.template.subs(thetas)
        self.template.subs({'cov': covariate})

        template_str = [str(symbol) for symbol in self.template.free_symbols]
        if 'mean' in template_str:
            self.template.subs({'mean': f'{covariate}_MEAN'})
            self.statistic_type = 'mean'
        elif 'median' in template_str:
            self.template.subs({'median': f'{covariate}_MEDIAN'})
            self.statistic_type = 'median'
        elif 'std' in template_str:
            self.template.subs({'std': f'{covariate}_STD'})
            self.statistic_type = 'std'

    def create_effect_statement(self, operation_str, statement_original):
        """Creates statement for addition or multiplication of covariate
        to parameter, e.g. (if parameter is CL and covariate is WGT):

            CL = CLWGT + TVCL*EXP(ETA(1))"""
        operation = self._get_operation(operation_str)

        symbol = statement_original.symbol
        expression = statement_original.expression

        statement_new = Assignment(symbol, operation(expression, self.template.symbol))

        return statement_new

    def create_statistics_statement(self, covariate, mean, median, std):
        """Creates statement where value of mean/median is explicit."""
        if self.statistic_type == 'mean':
            return Assignment(S(f'{covariate}_MEAN'), Float(mean, 6))
        elif self.statistic_type == 'median':
            return Assignment(S(f'{covariate}_MEDIAN'), Float(median, 6))
        elif self.statistic_type == 'std':
            return Assignment(S(f'{covariate}_STD'), Float(std, 6))

    @staticmethod
    def _get_operation(operation_str):
        """Gets sympy operation based on string"""
        if operation_str == '*':
            return mul
        elif operation_str == '+':
            return add

    @classmethod
    def linear(cls):
        """Linear continuous template (for continuous covariates)."""
        symbol = S('symbol')
        expression = 1 + S('theta') * (S('cov') - S('median'))
        template = Assignment(symbol, expression)

        return cls(template)

    @classmethod
    def categorical(cls, counts):
        """Linear categorical template (for categorical covariates)."""
        symbol = S('symbol')
        most_common = counts[counts.idxmax()]
        categories = counts.unique()

        values = [1]

        if np.isnan(most_common):
            most_common = S('NaN')

        conditions = [Eq(S('cov'), most_common)]

        for i, cat in enumerate(categories):
            if cat != most_common:
                values += [1 + S(f'theta{i}')]
                if np.isnan(cat):
                    conditions += [Eq(S('cov'), S('NaN'))]
                else:
                    conditions += [Eq(S('cov'), cat)]

        expression = Piecewise(*zip(values, conditions))

        template = Assignment(symbol, expression)

        return cls(template)

    @classmethod
    def piecewise_linear(cls):
        """Piecewise linear ("hockey-stick") template (for continuous
        covariates)."""
        symbol = S('symbol')
        values = [1 + S('theta1') * (S('cov') - S('median')),
                  1 + S('theta2') * (S('cov') - S('median'))]
        conditions = [Le(S('cov'), S('median')),
                      Gt(S('cov'), S('median'))]
        expression = Piecewise((values[0], conditions[0]),
                               (values[1], conditions[1]))

        template = Assignment(symbol, expression)

        return cls(template)

    @classmethod
    def exponential(cls):
        """Exponential template (for continuous covariates)."""
        symbol = S('symbol')
        expression = exp(S('theta') * (S('cov') - S('median')))
        template = Assignment(symbol, expression)

        return cls(template)

    @classmethod
    def power(cls):
        """Power template (for continuous covariates)."""
        symbol = S('symbol')
        expression = (S('cov')/S('median'))**S('theta')
        template = Assignment(symbol, expression)

        return cls(template)

    def __str__(self):
        """String representation of class."""
        return str(self.template)
