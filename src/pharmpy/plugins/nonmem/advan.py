from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable

from pharmpy.deps import pandas as pd
from pharmpy.deps import sympy
from pharmpy.expressions import canonical_ode_rhs
from pharmpy.model import (
    Assignment,
    Bolus,
    Compartment,
    CompartmentalSystem,
    CompartmentalSystemBuilder,
    DataInfo,
    ExplicitODESystem,
    Infusion,
    ModelSyntaxError,
)

if TYPE_CHECKING:
    from .model import Model

from .nmtran_parser import NMTranControlStream


def compartmental_model(model: Model, advan: str, trans, des=None):
    return _compartmental_model(model, model.internals.control_stream, advan, trans, des)


def _compartmental_model(
    model: Model, control_stream: NMTranControlStream, advan: str, trans, des=None
):
    if advan == 'ADVAN1':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        central = Compartment(
            'CENTRAL', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(central)
        cb.add_compartment(output)
        cb.add_flow(central, output, _advan1and2_trans(trans))
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'CENTRAL': 1, 'OUTPUT': 2}
    elif advan == 'ADVAN2':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        depot = Compartment(
            'DEPOT', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        central = Compartment(
            'CENTRAL', None, _get_alag(control_stream, 2), _get_bioavailability(control_stream, 2)
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(depot)
        cb.add_compartment(central)
        cb.add_compartment(output)
        cb.add_flow(central, output, _advan1and2_trans(trans))
        cb.add_flow(depot, central, sympy.Symbol('KA'))
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'DEPOT': 1, 'CENTRAL': 2, 'OUTPUT': 3}
    elif advan == 'ADVAN3':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        central = Compartment(
            'CENTRAL', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        peripheral = Compartment(
            'PERIPHERAL',
            None,
            _get_alag(control_stream, 2),
            _get_bioavailability(control_stream, 2),
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(central)
        cb.add_compartment(peripheral)
        cb.add_compartment(output)
        k, k12, k21 = _advan3_trans(trans)
        cb.add_flow(central, output, k)
        cb.add_flow(central, peripheral, k12)
        cb.add_flow(peripheral, central, k21)
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'CENTRAL': 1, 'PERIPHERAL': 2, 'OUTPUT': 3}
    elif advan == 'ADVAN4':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        depot = Compartment(
            'DEPOT', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        central = Compartment(
            'CENTRAL', None, _get_alag(control_stream, 2), _get_bioavailability(control_stream, 2)
        )
        peripheral = Compartment(
            'PERIPHERAL',
            None,
            _get_alag(control_stream, 3),
            _get_bioavailability(control_stream, 3),
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(depot)
        cb.add_compartment(central)
        cb.add_compartment(peripheral)
        cb.add_compartment(output)
        k, k23, k32, ka = _advan4_trans(trans)
        cb.add_flow(depot, central, ka)
        cb.add_flow(central, output, k)
        cb.add_flow(central, peripheral, k23)
        cb.add_flow(peripheral, central, k32)
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'DEPOT': 1, 'CENTRAL': 2, 'PERIPHERAL': 3, 'OUTPUT': 4}
    elif advan == 'ADVAN5' or advan == 'ADVAN7':
        cb = CompartmentalSystemBuilder()
        modrec = control_stream.get_records('MODEL')[0]
        defobs = None
        defdose = None
        central = None
        depot = None
        first_dose = None
        compartments = []
        comp_names = []
        dose_no = -1
        depot_no = -1
        first_dose_no = -1
        for i, (name, opts) in enumerate(modrec.compartments(), 1):
            if 'DEFOBSERVATION' in opts:
                defobs = name
            if 'DEFDOSE' in opts:
                defdose = name
                dose_no = i
            if name == 'CENTRAL':
                central = name
            elif name == 'DEPOT':
                depot = name
                depot_no = i
            if first_dose is None and 'NODOSE' not in opts:
                first_dose = name
                first_dose_no = i
            comp_names.append(name)

        comp_names.append('OUTPUT')
        comp_map = {name: i for i, name in enumerate(comp_names, 1)}

        if not defobs:
            if central:
                defobs = central
            else:
                defobs = comp_names[0]

        if not defdose:
            if depot:
                defdose = depot
                dose_no = depot_no
            elif first_dose is not None:
                defdose = first_dose
                dose_no = first_dose_no
            else:
                raise ModelSyntaxError('Dosing compartment is unknown')

        assert dose_no != -1

        dose = _dosing(model, control_stream, dose_no)
        for i, name in enumerate(comp_names):
            if i == len(comp_names) - 1:
                output = Compartment(name)
                cb.add_compartment(output)
                compartments.append(output)
                break
            if name == defdose:
                curdose = dose
            else:
                curdose = None
            comp = Compartment(
                name, curdose, _get_alag(control_stream, i), _get_bioavailability(control_stream, i)
            )
            cb.add_compartment(comp)
            compartments.append(comp)
            if name == defobs:
                defobs = comp
        for from_n, to_n, rate in _find_rates(control_stream, len(comp_names)):
            cb.add_flow(compartments[from_n - 1], compartments[to_n - 1], rate)
        ass = _f_link_assignment(control_stream, defobs)
    elif advan == 'ADVAN10':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        central = Compartment(
            'CENTRAL', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(central)
        cb.add_compartment(output)
        vm = sympy.Symbol('VM')
        km = sympy.Symbol('KM')
        t = sympy.Symbol('t')
        cb.add_flow(central, output, vm / (km + sympy.Function(central.amount.name)(t)))
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'CENTRAL': 1, 'OUTPUT': 2}
    elif advan == 'ADVAN11':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        central = Compartment(
            'CENTRAL', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        per1 = Compartment(
            'PERIPHERAL1',
            None,
            _get_alag(control_stream, 2),
            _get_bioavailability(control_stream, 2),
        )
        per2 = Compartment(
            'PERIPHERAL2',
            None,
            _get_alag(control_stream, 3),
            _get_bioavailability(control_stream, 3),
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(central)
        cb.add_compartment(per1)
        cb.add_compartment(per2)
        cb.add_compartment(output)
        k, k12, k21, k13, k31 = _advan11_trans(trans)
        cb.add_flow(central, output, k)
        cb.add_flow(central, per1, k12)
        cb.add_flow(per1, central, k21)
        cb.add_flow(central, per2, k13)
        cb.add_flow(per2, central, k31)
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'CENTRAL': 1, 'PERIPHERAL1': 2, 'PERIPHERAL2': 3, 'OUTPUT': 4}
    elif advan == 'ADVAN12':
        dose = _dosing(model, control_stream, 1)
        cb = CompartmentalSystemBuilder()
        depot = Compartment(
            'DEPOT', dose, _get_alag(control_stream, 1), _get_bioavailability(control_stream, 1)
        )
        central = Compartment(
            'CENTRAL', None, _get_alag(control_stream, 2), _get_bioavailability(control_stream, 2)
        )
        per1 = Compartment(
            'PERIPHERAL1',
            None,
            _get_alag(control_stream, 3),
            _get_bioavailability(control_stream, 3),
        )
        per2 = Compartment(
            'PERIPHERAL2',
            None,
            _get_alag(control_stream, 4),
            _get_bioavailability(control_stream, 4),
        )
        output = Compartment('OUTPUT')
        cb.add_compartment(depot)
        cb.add_compartment(central)
        cb.add_compartment(per1)
        cb.add_compartment(per2)
        cb.add_compartment(output)
        k, k23, k32, k24, k42, ka = _advan12_trans(trans)
        cb.add_flow(depot, central, ka)
        cb.add_flow(central, output, k)
        cb.add_flow(central, per1, k23)
        cb.add_flow(per1, central, k32)
        cb.add_flow(central, per2, k24)
        cb.add_flow(per2, central, k42)
        ass = _f_link_assignment(control_stream, central)
        comp_map = {'DEPOT': 1, 'CENTRAL': 2, 'PERIPHERAL1': 3, 'PERIPHERAL2': 4, 'OUTPUT': 5}
    elif des:
        rec_model = control_stream.get_records('MODEL')[0]

        subs_dict, comp_names = dict(), dict()
        comps = [c for c, _ in rec_model.compartments()]

        t = sympy.Symbol('t')
        for i, c in enumerate(comps, 1):
            a = sympy.Function(f'A_{c}')
            subs_dict[f'DADT({i})'] = sympy.Derivative(a(t))
            subs_dict[f'A({i})'] = a(t)
            comp_names[f'A({i})'] = a

        sset = des.statements.subs(subs_dict)

        a_out = sympy.Function('A_OUTPUT')
        dose = _dosing(model, control_stream, 1)

        ics = {v(0): sympy.Integer(0) for v in comp_names.values()}
        ics[a_out(0)] = sympy.Integer(0)
        ics[comp_names['A(1)'](0)] = dose.amount

        dadt_dose = sset.find_assignment(subs_dict['DADT(1)'])

        if len(comps) > 1:
            dadt_rest = [
                sympy.Eq(s.symbol, s.expression)
                for s in sset
                if s != dadt_dose and not s.symbol.is_Symbol
            ]
            lhs_sum = dadt_dose.expression
            for eq in dadt_rest:
                lhs_sum += eq.rhs
            dadt_out = sympy.Eq(sympy.Derivative(a_out(t)), canonical_ode_rhs(-lhs_sum))
            dadt_rest.append(dadt_out)
        else:
            dadt_rest = [sympy.Eq(sympy.Derivative(a_out(t)), dadt_dose.expression * -1)]

        if isinstance(dose, Infusion):
            if dose.duration:
                rate = dose.amount / dose.duration
                duration = dose.duration
            else:
                rate = dose.rate
                duration = dose.amount / dose.rate

            dadt_dose.expression += sympy.Piecewise((rate, duration > t), (0, True))
            ics[comp_names['A(1)'](0)] = sympy.Integer(0)

        eqs = [sympy.Eq(dadt_dose.symbol, dadt_dose.expression)] + dadt_rest

        ode = ExplicitODESystem(eqs, ics)
        ass = _f_link_assignment(control_stream, sympy.Symbol('A_CENTRAL'))
        return ode, ass
    else:
        return None
    model._compartment_map = comp_map
    return CompartmentalSystem(cb), ass


def _f_link_assignment(control_stream: NMTranControlStream, compartment):
    f = sympy.Symbol('F')
    try:
        fexpr = compartment.amount
    except AttributeError:
        fexpr = compartment
    pkrec = control_stream.get_records('PK')[0]
    if pkrec.statements.find_assignment('S1'):
        fexpr = fexpr / sympy.Symbol('S1')
    ass = Assignment(f, fexpr)
    return ass


def _find_rates(control_stream: NMTranControlStream, ncomps: int):
    pkrec = control_stream.get_records('PK')[0]
    for stat in pkrec.statements:
        if hasattr(stat, 'symbol'):
            name = stat.symbol.name
            m = re.match(r'^K(\d+)(T\d+)?$', name)
            if m:
                if m.group(2):
                    from_n = int(m.group(1))
                    to_n = int(m.group(2)[1:])
                else:
                    n = m.group(1)
                    if len(n) == 2:
                        from_n = int(n[0])
                        to_n = int(n[1])
                    elif len(n) == 3:
                        f1 = int(n[0])
                        t1 = int(n[1:])
                        f2 = int(n[:2])
                        t2 = int(n[2:])
                        q1 = f1 <= ncomps and t1 <= ncomps and t1 != 0
                        q2 = f2 <= ncomps and t2 <= ncomps
                        if q1 and q2:
                            raise ModelSyntaxError(
                                f'Rate parameter {n} is ambiguous. Use the KiTj notation.'
                            )
                        if q1:
                            from_n = f1
                            to_n = t1
                        elif q2:
                            from_n = f2
                            to_n = t2
                        else:
                            # Too large to or from compartment index. What would NONMEM do?
                            # Could also be too large later
                            continue
                    elif len(n) == 4:
                        from_n = int(n[:2])
                        to_n = int(n[2:])
                    else:
                        raise ValueError(f'Cannot handle {n}.')

                if to_n == 0:
                    to_n = ncomps

                yield from_n, to_n, sympy.Symbol(name)


def _advan1and2_trans(trans: str):
    if trans == 'TRANS2':
        return sympy.Symbol('CL') / sympy.Symbol('V')
    else:  # TRANS1 which is also the default
        return sympy.Symbol('K')


def _advan3_trans(trans: str):
    if trans == 'TRANS3':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V'),
            sympy.Symbol('Q') / sympy.Symbol('V'),
            sympy.Symbol('Q') / (sympy.Symbol('VSS') - sympy.Symbol('V')),
        )
    elif trans == 'TRANS4':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V1'),
            sympy.Symbol('Q') / sympy.Symbol('V1'),
            sympy.Symbol('Q') / sympy.Symbol('V2'),
        )
    elif trans == 'TRANS5':
        return (
            sympy.Symbol('ALPHA') * sympy.Symbol('BETA') / sympy.Symbol('K21'),
            sympy.Symbol('ALPHA') + sympy.Symbol('BETA') - sympy.Symbol('K21') - sympy.Symbol('K'),
            (sympy.Symbol('AOB') * sympy.Symbol('BETA') + sympy.Symbol('ALPHA'))
            / (sympy.Symbol('AOB') + 1),
        )
    elif trans == 'TRANS6':
        return (
            sympy.Symbol('ALPHA') * sympy.Symbol('BETA') / sympy.Symbol('K21'),
            sympy.Symbol('ALPHA') + sympy.Symbol('BETA') - sympy.Symbol('K21') - sympy.Symbol('K'),
            sympy.Symbol('K21'),
        )
    else:
        return (sympy.Symbol('K'), sympy.Symbol('K12'), sympy.Symbol('K21'))


def _advan4_trans(trans: str):
    if trans == 'TRANS3':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V'),
            sympy.Symbol('Q') / sympy.Symbol('V'),
            sympy.Symbol('Q') / (sympy.Symbol('VSS') - sympy.Symbol('V')),
            sympy.Symbol('KA'),
        )
    elif trans == 'TRANS4':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V2'),
            sympy.Symbol('Q') / sympy.Symbol('V2'),
            sympy.Symbol('Q') / sympy.Symbol('V3'),
            sympy.Symbol('KA'),
        )
    elif trans == 'TRANS5':
        return (
            sympy.Symbol('ALPHA') * sympy.Symbol('BETA') / sympy.Symbol('K32'),
            sympy.Symbol('ALPHA') + sympy.Symbol('BETA') - sympy.Symbol('K32') - sympy.Symbol('K'),
            (sympy.Symbol('AOB') * sympy.Symbol('BETA') + sympy.Symbol('ALPHA'))
            / (sympy.Symbol('AOB') + 1),
            sympy.Symbol('KA'),
        )
    elif trans == 'TRANS6':
        return (
            sympy.Symbol('ALPHA') * sympy.Symbol('BETA') / sympy.Symbol('K32'),
            sympy.Symbol('ALPHA') + sympy.Symbol('BETA') - sympy.Symbol('K32') - sympy.Symbol('K'),
            sympy.Symbol('K32'),
            sympy.Symbol('KA'),
        )
    else:
        return (sympy.Symbol('K'), sympy.Symbol('K23'), sympy.Symbol('K32'), sympy.Symbol('KA'))


def _advan11_trans(trans: str):
    if trans == 'TRANS4':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V1'),
            sympy.Symbol('Q2') / sympy.Symbol('V1'),
            sympy.Symbol('Q2') / sympy.Symbol('V2'),
            sympy.Symbol('Q3') / sympy.Symbol('V1'),
            sympy.Symbol('Q3') / sympy.Symbol('V3'),
        )
    elif trans == 'TRANS6':
        return (
            sympy.Symbol('ALPHA')
            * sympy.Symbol('BETA')
            * sympy.Symbol('GAMMA')
            / (sympy.Symbol('K21') * sympy.Symbol('K31')),
            sympy.Symbol('ALPHA')
            + sympy.Symbol('BETA')
            + sympy.Symbol('GAMMA')
            - sympy.Symbol('K')
            - sympy.Symbol('K13')
            - sympy.Symbol('K21')
            - sympy.Symbol('K31'),
            sympy.Symbol('K21'),
            (
                sympy.Symbol('ALPHA') * sympy.Symbol('BETA')
                + sympy.Symbol('ALPHA') * sympy.Symbol('GAMMA')
                + sympy.Symbol('BETA') * sympy.Symbol('GAMMA')
                + sympy.Symbol('K31') * sympy.Symbol('K31')
                - sympy.Symbol('K31')
                * (sympy.Symbol('ALPHA') + sympy.Symbol('BETA') + sympy.Symbol('GAMMA'))
                - sympy.Symbol('K') * sympy.Symbol('K21')
            )
            / (sympy.Symbol('K21') - sympy.Symbol('K31')),
            sympy.Symbol('K31'),
        )
    else:
        return (
            sympy.Symbol('K'),
            sympy.Symbol('K12'),
            sympy.Symbol('K21'),
            sympy.Symbol('K13'),
            sympy.Symbol('K31'),
        )


def _advan12_trans(trans: str):
    if trans == 'TRANS4':
        return (
            sympy.Symbol('CL') / sympy.Symbol('V2'),
            sympy.Symbol('Q3') / sympy.Symbol('V2'),
            sympy.Symbol('Q3') / sympy.Symbol('V3'),
            sympy.Symbol('Q4') / sympy.Symbol('V2'),
            sympy.Symbol('Q4') / sympy.Symbol('V4'),
            sympy.Symbol('KA'),
        )
    elif trans == 'TRANS6':
        return (
            sympy.Symbol('ALPHA')
            * sympy.Symbol('BETA')
            * sympy.Symbol('GAMMA')
            / (sympy.Symbol('K32') * sympy.Symbol('K42')),
            sympy.Symbol('ALPHA')
            + sympy.Symbol('BETA')
            + sympy.Symbol('GAMMA')
            - sympy.Symbol('K')
            - sympy.Symbol('K24')
            - sympy.Symbol('K32')
            - sympy.Symbol('K42'),
            sympy.Symbol('K32'),
            (
                sympy.Symbol('ALPHA') * sympy.Symbol('BETA')
                + sympy.Symbol('ALPHA') * sympy.Symbol('GAMMA')
                + sympy.Symbol('BETA') * sympy.Symbol('GAMMA')
                + sympy.Symbol('K42') * sympy.Symbol('K42')
                - sympy.Symbol('K42')
                * (sympy.Symbol('ALPHA') + sympy.Symbol('BETA') + sympy.Symbol('GAMMA'))
                - sympy.Symbol('K') * sympy.Symbol('K32')
            )
            / (sympy.Symbol('K32') - sympy.Symbol('K42')),
            sympy.Symbol('K42'),
            sympy.Symbol('KA'),
        )
    else:
        return (
            sympy.Symbol('K'),
            sympy.Symbol('K23'),
            sympy.Symbol('K32'),
            sympy.Symbol('K24'),
            sympy.Symbol('K42'),
            sympy.Symbol('KA'),
        )


def _dosing(model: Model, control_stream: NMTranControlStream, dose_comp: int):
    # FIXME Use lambda: model.dataset instead
    def dataset():
        return model._read_dataset(control_stream, raw=False)

    return dosing(model.datainfo, dataset, dose_comp)


def dosing(di: DataInfo, dataset: Callable[[], pd.DataFrame], dose_comp: int):
    colnames = di.names

    if 'RATE' in colnames and not di['RATE'].drop:
        df = dataset()
        if (df['RATE'] == 0).all():
            return Bolus(sympy.Symbol('AMT'))
        elif (df['RATE'] == -1).any():
            return Infusion(sympy.Symbol('AMT'), rate=sympy.Symbol(f'R{dose_comp}'))
        elif (df['RATE'] == -2).any():
            return Infusion(sympy.Symbol('AMT'), duration=sympy.Symbol(f'D{dose_comp}'))
        else:
            return Infusion(sympy.Symbol('AMT'), rate=sympy.Symbol('RATE'))
    else:
        return Bolus(sympy.Symbol('AMT'))


def _get_alag(control_stream: NMTranControlStream, n: int):
    """Check if ALAGn is defined in model and return it else return 0"""
    alag = f'ALAG{n}'
    pkrec = control_stream.get_records('PK')[0]
    if pkrec.statements.find_assignment(alag):
        return sympy.Symbol(alag)
    else:
        return sympy.Integer(0)


def _get_bioavailability(control_stream: NMTranControlStream, n: int):
    """Check if Fn is defined in model and return it else return 0"""
    fn = f'F{n}'
    pkrec = control_stream.get_records('PK')[0]
    if pkrec.statements.find_assignment(fn):
        return sympy.Symbol(fn)
    else:
        return sympy.Integer(1)
