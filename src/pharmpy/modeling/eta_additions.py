"""
:meta private:
"""

from operator import add, mul

import sympy
import sympy.stats as stats
from sympy import Eq, Piecewise

from pharmpy.parameter import Parameter
from pharmpy.random_variables import VariabilityLevel
from pharmpy.statements import Assignment, ModelStatements
from pharmpy.symbols import symbol as S


def add_iiv(model, list_of_parameters, expression, operation='*', eta_names=None):
    """
    Adds IIVs to :class:`pharmpy.model`. Effects that currently have templates are:

    - Additive (*add*)
    - Proportional (*prop*)
    - Exponential (*exp*)
    - Logit (*logit*)

    For all except exponential the operation input is not needed. Otherwise user specified
    input is supported. Initial estimates for new etas are 0.09.

    Parameters
    ----------
    model : Model
        Pharmpy model to add new IIVs to.
    list_of_parameters : str, list
        Name/names of parameter to add new IIVs to.
    expression : str, list
        Effect/effects on eta. Either abbreviated (see above) or custom.
    operation : str, list, optional
        Whether the new IIV should be added or multiplied (default).
    eta_names: str, list, optional
        Custom name/names of new eta
    """
    rvs, pset, sset = model.random_variables, model.parameters, model.statements

    parameter, additional_options = _change_to_list(
        list_of_parameters, [expression, operation, eta_names]
    )
    expression, operation, eta_names = additional_options

    if all(eta_name is None for eta_name in eta_names):
        eta_names = None

    if not all(len(opt) == len(parameter) for opt in additional_options if opt):
        raise ValueError(
            'The number of provided expressions and operations must either be equal '
            'to the number of parameters or equal to 1'
        )

    for i in range(len(parameter)):
        omega = S(f'IIV_{parameter[i]}')
        if not eta_names:
            eta_name = f'ETA_{parameter[i]}'
        else:
            eta_name = eta_names[i]

        eta = stats.Normal(eta_name, 0, sympy.sqrt(omega))
        eta.variability_level = VariabilityLevel.IIV

        rvs.add(eta)
        pset.add(Parameter(str(omega), init=0.09))

        statement = sset.find_assignment(parameter[i])

        eta_addition = _create_template(expression[i], operation[i])
        eta_addition.apply(statement.expression, eta.name)

        statement.expression = eta_addition.template

    model.random_variables = rvs
    model.parameters = pset
    model.statements = sset

    return model


def add_iov(model, occ, list_of_parameters=None, eta_names=None):
    """
    Adds IOVs to :class:`pharmpy.model`. Initial estimate of new IOVs are 10% of the IIV eta
    it is based on.

    Parameters
    ----------
    model : Model
        Pharmpy model to add new IOVs to.
    occ : str
        Name of occasion column.
    list_of_parameters : str, list
        List of names of parameters and random variables. Accepts random variable names, parameter
        names, or a mix of both.
    eta_names: list
        Custom names of new etas. Must be equal to the number of input etas times the number of
        categories for occasion.
    """
    rvs, pset, sset = model.random_variables, model.parameters, model.statements
    params, additional_options = _change_to_list(list_of_parameters, [eta_names])

    if len(additional_options) > 1:
        eta_names = additional_options[0]

    etas = _get_etas(rvs, sset, params)
    iovs, etais = ModelStatements(), ModelStatements()

    categories = _get_occ_levels(model.dataset, occ)

    if eta_names and len(eta_names) != len(etas) * len(categories):
        raise ValueError(
            f'Number of provided names incorrect, need {len(etas) * len(categories)} names.'
        )
    elif len(categories) == 1:
        raise ValueError(f'Only one value in {occ} column.')

    for i, eta in enumerate(etas, 1):
        omega_name = str(next(iter(eta.pspace.distribution.free_symbols)))
        omega = S(f'OMEGA_IOV_{i}')  # TODO: better name
        pset.add(Parameter(str(omega), init=pset[omega_name].init * 0.1))

        iov = S(f'IOV_{i}')

        values, conditions = [], []

        for j, cat in enumerate(categories, 1):
            if eta_names:
                eta_name = eta_names[j - 1]
            else:
                eta_name = f'ETA_IOV_{i}{j}'

            eta_new = stats.Normal(eta_name, 0, sympy.sqrt(omega))
            eta_new.variability_level = VariabilityLevel.IOV

            rvs.add(eta_new)

            values += [S(eta_new.name)]
            conditions += [Eq(cat, S(occ))]

        expression = Piecewise(*zip(values, conditions))

        iovs.append(Assignment(iov, sympy.sympify(0)))
        iovs.append(Assignment(iov, expression))
        etais.append(Assignment(S(f'ETAI{i}'), eta + iov))

        sset.subs({eta.name: S(f'ETAI{i}')})

    iovs.extend(etais)
    iovs.extend(sset)

    model.random_variables, model.parameters, model.statements = rvs, pset, iovs

    return model


def _change_to_list(list_of_params, additional_options):
    additional_options_as_list = []
    params = list_of_params

    if isinstance(list_of_params, list):
        for option in additional_options:
            if isinstance(option, str) or not option:
                option = [option] * len(list_of_params)
            additional_options_as_list.append(option)
    else:
        if list_of_params:
            params = [list_of_params]
        for option in additional_options:
            additional_options_as_list.append([option])

    return params, additional_options_as_list


def _create_template(expression, operation):
    operation_func = _get_operation_func(operation)
    if expression == 'add':
        return EtaAddition.additive()
    elif expression == 'prop':
        return EtaAddition.proportional()
    elif expression == 'exp':
        return EtaAddition.exponential(operation_func)
    elif expression == 'log':
        return EtaAddition.logit()
    else:
        expression = sympy.sympify(f'original {operation} {expression}')
        return EtaAddition(expression)


def _get_operation_func(operation):
    """Gets sympy operation based on string"""
    if operation == '*':
        return mul
    elif operation == '+':
        return add


def _get_etas(rvs, sset, list_of_parameters):
    if list_of_parameters is None:
        return rvs.etas
    else:
        etas = []
        for param in list_of_parameters:
            try:
                if rvs[param.upper()] not in etas:
                    etas.append(rvs[param.upper()])
            except KeyError:
                try:
                    exp_symbs = sset.find_assignment(param).expression.free_symbols
                except AttributeError:
                    raise KeyError(f'Symbol/random variable "{param}" does not exist')
                etas += [
                    rvs[str(e)]
                    for e in exp_symbs.intersection(rvs.free_symbols)
                    if rvs[str(e)] not in etas
                ]
        return etas


def _get_occ_levels(df, occ):
    levels = df[occ].unique()
    return _round_categories(levels)


def _round_categories(categories):
    categories_rounded = []
    for c in categories:
        if not isinstance(c, int) or c.is_integer():
            categories_rounded.append(int(c))
        else:
            categories_rounded.append(c)
    categories_rounded.sort()
    return categories_rounded


class EtaAddition:
    """
    Eta addition consisting of expression.

    Attributes
    ----------
    template
        Expression consisting of original statement +/* new eta with effect.

    :meta private:

    """

    def __init__(self, template):
        self.template = template

    def apply(self, original, eta):
        self.template = self.template.subs({'original': original, 'eta_new': eta})

    @classmethod
    def additive(cls):
        template = S('original') + S('eta_new')

        return cls(template)

    @classmethod
    def proportional(cls):
        template = S('original') * S('eta_new')

        return cls(template)

    @classmethod
    def exponential(cls, operation):
        template = operation(S('original'), sympy.exp(S('eta_new')))

        return cls(template)

    @classmethod
    def logit(cls):
        template = S('original') * (sympy.exp(S('eta_new')) / (1 + sympy.exp(S('eta_new'))))

        return cls(template)
